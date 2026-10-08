import { screen, within } from '@testing-library/react';
import { Children, isValidElement, type ReactNode } from 'react';
import { Route } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';
import { fakePortfolio } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { renderApp } from '@/test/render';
import { fakeSession, server, TEST_USERNAME } from '@/test/server';
import { VALUED_SUMMARY } from '@/test/summaryFixtures';

/**
 * Criterion 6 of #16: no credential input exists anywhere in the UI.
 *
 * CLAUDE.md rule 3: credentials are read from environment variables on the
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

function passwordInputs(): Element[] {
  return Array.from(document.body.querySelectorAll('input')).filter(
    (input) => input.type === 'password',
  );
}

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
    ready: () => screen.findByRole('region', { name: 'Holdings table' }),
    passwordInputs: 0,
  },
  {
    route: '/details',
    visit: '/details',
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

function serve(signedIn: boolean): void {
  const session = fakeSession(signedIn ? { initialUser: TEST_USERNAME } : {});
  const scenario = healthyPortfolio();
  server.use(
    ...session.handlers,
    ...fakePortfolio({ ...scenario, summary: VALUED_SUMMARY, session }).handlers,
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

  it('the check itself fires on a control named like a key', () => {
    // The positive control: a check that never matches passes on a page full
    // of key fields.
    document.body.innerHTML = `
      <label for="provider-api-key">API key</label><input id="provider-api-key" />
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
      '<label>Signing key <input /></label>',
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
