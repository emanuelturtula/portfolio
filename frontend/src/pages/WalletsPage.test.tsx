import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ADDRESS_REJECTIONS,
  DUPLICATE_ARCHIVED_DETAIL,
  DUPLICATE_DETAIL,
  fakePortfolio,
  LABEL_TOO_LONG,
  validationProblem,
  WALLET_NOT_FOUND_DETAIL,
  WALLET_PATH,
  WALLETS_PATH,
  type FakePortfolio,
  type FakePortfolioOptions,
} from '@/test/fakePortfolio';
import { ADDRESSES, wallet, type WalletResponse } from '@/test/fixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';

/**
 * The action half of a per-row control's accessible name (R8). Every row's
 * control is named for its row - "Archive Cold storage" - and these match the
 * action within one row; the tests under "row names" pin the full names.
 */
const ARCHIVE = /^Archive /;
const CONFIRM_ARCHIVE = /^Confirm archive of /;
const CANCEL_ARCHIVE = /^Cancel archiving /;
const RESTORE = /^Restore /;
const COPY_ADDRESS = /^Copy address of /;

/**
 * The owner's registry for most tests: two Bitcoin wallets, one labelled and
 * one not, and a Kaspa wallet.
 */
function threeWallets(): WalletResponse[] {
  return [
    wallet({ id: 1, chain_key: 'bitcoin', address: ADDRESSES.btcSegwit, label: 'Cold storage' }),
    wallet({ id: 2, chain_key: 'bitcoin', address: ADDRESSES.btcLegacy, label: null }),
    wallet({ id: 3, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Mining payouts' }),
  ];
}

interface Setup {
  readonly user: UserEvent;
  readonly fake: FakePortfolio;
}

/**
 * Signs in, installs the fake backend and opens `/wallets`.
 *
 * `overrides` are installed after the fake and so take precedence over it,
 * and they are in place before the first render: an override registered after
 * `renderApp` only wins because the session read happens to come first.
 */
function openWalletsPage(
  options: FakePortfolioOptions = {},
  overrides: readonly HttpHandler[] = [],
): Setup {
  const user = userEvent.setup();
  const fake = fakePortfolio(options);
  server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers, ...fake.handlers);
  server.use(...overrides);

  renderApp(['/wallets']);

  return { user, fake };
}

/** An empty registry, with the page loaded and the form ready. */
async function openEmptyWalletsPage(overrides: readonly HttpHandler[] = []): Promise<Setup> {
  const setup = openWalletsPage({ wallets: [] }, overrides);
  await screen.findByRole('heading', { name: /no wallets yet/i });
  return setup;
}

/**
 * The outline level of a heading, from `aria-level` or its `h1`-`h6` tag. The
 * list section's own heading is an `h3` ("Your wallets"), so anything inside it
 * has to sit below that for the page outline to stay a tree (R7).
 */
function headingLevel(heading: HTMLElement): number {
  const explicit = heading.getAttribute('aria-level');
  if (explicit !== null) {
    return Number(explicit);
  }
  const match = /^H([1-6])$/.exec(heading.tagName);
  if (match?.[1] === undefined) {
    throw new Error(`<${heading.tagName.toLowerCase()}> is not a heading.`);
  }
  return Number(match[1]);
}

/** The list region, once it has loaded. */
async function walletList(): Promise<HTMLElement> {
  const region = await screen.findByRole('region', { name: 'Your wallets' });
  return within(region).findByRole('list');
}

/** The list item whose text contains `text`. Fails loudly when there is none. */
async function rowFor(text: string): Promise<HTMLElement> {
  const list = await walletList();
  const row = within(list)
    .getAllByRole('listitem')
    .find((item) => within(item).queryByText(text) !== null);

  if (row === undefined) {
    throw new Error(`No wallet row contains "${text}". List was: ${list.textContent}`);
  }

  return row;
}

/** The truncated address `<Address>` renders, by the full address in its title. */
function shownAddress(container: HTMLElement, address: string): HTMLElement {
  return within(container).getByTitle(address);
}

function addForm(): HTMLElement {
  return screen.getByRole('form', { name: 'Add a wallet' });
}

function addressInput(): HTMLElement {
  return within(addForm()).getByLabelText('Address');
}

function labelInput(): HTMLElement {
  return within(addForm()).getByLabelText('Label');
}

function chainSelect(): HTMLElement {
  return within(addForm()).getByLabelText('Chain');
}

function submitButton(): HTMLElement {
  return within(addForm()).getByRole('button', { name: 'Add wallet' });
}

/** Every `POST /api/wallets` the fake saw. */
function creates(fake: FakePortfolio) {
  return fake.writes('POST', WALLETS_PATH);
}

/**
 * Asserts that `message` is rendered as `input`'s own error: marked invalid,
 * and reachable through `aria-describedby`, which is what a screen reader
 * reads out on focus.
 */
function expectFieldError(input: HTMLElement, message: string | RegExp): void {
  expect(input).toHaveAttribute('aria-invalid', 'true');
  expect(input).toHaveAccessibleDescription(
    typeof message === 'string' ? expect.stringContaining(message) : message,
  );
}

function expectNoFieldError(input: HTMLElement, message: string): void {
  expect(input).not.toHaveAttribute('aria-invalid', 'true');
  expectNotDescribedBy(input, message);
}

/** `message` is not part of `input`'s accessible description. */
function expectNotDescribedBy(input: HTMLElement, message: string): void {
  expect(input).not.toHaveAccessibleDescription(expect.stringContaining(message));
}

describe('WalletsPage: list', () => {
  it("lists the owner's wallets with chain, label and address", async () => {
    openWalletsPage({ wallets: threeWallets() });

    const list = await walletList();
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);

    const cold = await rowFor('Cold storage');
    expect(cold).toHaveTextContent('Bitcoin');
    expect(shownAddress(cold, ADDRESSES.btcSegwit)).toHaveTextContent('tb1qw508…xpjzsx');

    const mining = await rowFor('Mining payouts');
    expect(mining).toHaveTextContent('Kaspa');
    expect(shownAddress(mining, ADDRESSES.kasPrimary)).toHaveTextContent('kaspatest:qxaqrl…gdmpks');

    // The unlabelled wallet is still listed, recognisable by its address.
    const unlabelled = within(list)
      .getAllByRole('listitem')
      .find((item) => within(item).queryByTitle(ADDRESSES.btcLegacy) !== null);
    expect(unlabelled).toBeDefined();
    expect(unlabelled).toHaveTextContent('Bitcoin');
    expect(unlabelled).not.toHaveTextContent('null');

    // Active wallets offer archive, not restore.
    expect(within(cold).getByRole('button', { name: ARCHIVE })).toBeInTheDocument();
    expect(within(cold).queryByRole('button', { name: RESTORE })).not.toBeInTheDocument();
    expect(within(cold).queryByText('Archived')).not.toBeInTheDocument();
  });

  it('lists a wallet on a chain this build does not know by its raw key', async () => {
    openWalletsPage({
      wallets: [wallet({ id: 9, chain_key: 'litecoin', address: 'tltc1qexample', label: 'Other' })],
    });

    const row = await rowFor('Other');
    expect(row).toHaveTextContent('litecoin');
    expect(within(row).getByRole('button', { name: ARCHIVE })).toBeInTheDocument();
  });

  it('names every row control for its row', async () => {
    // R8. Three rows of "Archive" and "Copy address" give a screen reader user
    // three identical controls to choose between. Each name carries the row's
    // label, or its chain and truncated address when it has none.
    openWalletsPage({ wallets: threeWallets() });
    await walletList();

    // Three archive buttons and three copy buttons, and each exact name
    // matches exactly one control on the page: together, one distinct name
    // per row, and no row's name shared with anything else.
    expect(screen.getAllByRole('button', { name: ARCHIVE })).toHaveLength(3);
    expect(screen.getAllByRole('button', { name: COPY_ADDRESS })).toHaveLength(3);
    for (const name of [
      'Archive Cold storage',
      'Archive Bitcoin mwgS2HRb…fFBmGq',
      'Archive Mining payouts',
      'Copy address of Cold storage',
      'Copy address of Bitcoin mwgS2HRb…fFBmGq',
      'Copy address of Mining payouts',
    ]) {
      expect(screen.getAllByRole('button', { name })).toHaveLength(1);
    }
  });

  it('names the confirm, cancel and restore controls for their row', async () => {
    const { user } = openWalletsPage({
      wallets: [
        ...threeWallets(),
        wallet({ id: 4, address: ADDRESSES.btcScript, label: 'Old exchange', archived: true }),
      ],
    });
    await walletList();

    await user.click(screen.getByRole('button', { name: 'Archive Cold storage' }));
    expect(screen.getByRole('button', { name: 'Confirm archive of Cold storage' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Cancel archiving Cold storage' })).toBeVisible();

    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    expect(await screen.findByRole('button', { name: 'Restore Old exchange' })).toBeVisible();
  });

  it('shows the full address nowhere but in the title until the owner asks', async () => {
    openWalletsPage({ wallets: threeWallets() });

    await walletList();

    for (const address of [ADDRESSES.btcSegwit, ADDRESSES.btcLegacy, ADDRESSES.kasPrimary]) {
      expect(screen.queryByText(address)).not.toBeInTheDocument();
    }
  });

  it('asks for the active wallets only, until archived ones are requested', async () => {
    const { fake } = openWalletsPage({ wallets: threeWallets() });

    await walletList();

    const reads = fake.requests.filter((entry) => entry.method === 'GET');
    expect(reads.length).toBeGreaterThan(0);
    for (const read of reads) {
      expect(new URL(read.url).searchParams.get('include_archived')).toBeNull();
    }
  });
});

describe('WalletsPage: states', () => {
  it('announces that the list is loading', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openWalletsPage({ wallets: threeWallets() }, [
      http.get(WALLETS_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);

    expect(await screen.findByText('Loading wallets…')).toHaveAttribute('role', 'status');
    // Not an empty state while it is still loading: "no wallets yet" would be a
    // claim about data that has not arrived.
    expect(screen.queryByText(/no wallets yet/i)).not.toBeInTheDocument();

    release();

    expect(await rowFor('Cold storage')).toBeInTheDocument();
    expect(screen.queryByText('Loading wallets…')).not.toBeInTheDocument();
  });

  it('no wallets: an empty state points at the add form', async () => {
    openWalletsPage({ wallets: [] });

    const region = await screen.findByRole('region', { name: 'Your wallets' });
    const empty = await within(region).findByRole('heading', { name: /no wallets yet/i });
    // R7: below the section's own h3, not a second h2 beside the page title.
    const sectionHeading = within(region).getByRole('heading', { name: 'Your wallets' });
    expect(headingLevel(sectionHeading)).toBe(3);
    expect(headingLevel(empty)).toBe(4);
    expect(within(region).queryByRole('list')).not.toBeInTheDocument();
    // An empty state is not a failure.
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
    // And the form it points at is there.
    expect(submitButton()).toBeEnabled();
  });

  it('a failed list load shows the reason and a retry, not an empty list', async () => {
    let failing = true;
    const { user } = openWalletsPage({ wallets: threeWallets() }, [
      // Falls through to the fake once `failing` is cleared.
      http.get(WALLETS_PATH, () =>
        failing ? problem(503, 'Service Unavailable', 'The database is not reachable.') : undefined,
      ),
    ]);

    const region = await screen.findByRole('region', { name: 'Your wallets' });
    const alert = await within(region).findByRole('alert');
    expect(alert).toHaveTextContent('Could not load your wallets');
    expect(alert).toHaveTextContent('The database is not reachable.');
    // R7: the error's heading sits below the section's h3.
    expect(headingLevel(within(alert).getByRole('heading'))).toBe(4);
    // A failure to read the list is not an empty list.
    expect(screen.queryByText(/no wallets yet/i)).not.toBeInTheDocument();

    failing = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await rowFor('Cold storage')).toBeInTheDocument();
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a list the backend cannot be reached for says so in words, not a status code', async () => {
    openWalletsPage({ wallets: threeWallets() }, [
      http.get(WALLETS_PATH, () => HttpResponse.error()),
    ]);

    const region = await screen.findByRole('region', { name: 'Your wallets' });
    const alert = await within(region).findByRole('alert');
    expect(alert).toHaveTextContent(/could not be reached/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('the add form still works when the list fails to load', async () => {
    const { user, fake } = openWalletsPage({ wallets: [] }, [
      http.get(WALLETS_PATH, () => problem(500, 'Internal Server Error', 'Boom.')),
    ]);

    const region = await screen.findByRole('region', { name: 'Your wallets' });
    await within(region).findByRole('alert');

    await user.type(addressInput(), ADDRESSES.btcRegtest);
    await user.type(labelInput(), 'Test rig');
    await user.click(submitButton());

    await waitFor(() => {
      expect(fake.wallets()).toHaveLength(1);
    });
    expect(fake.wallets()[0]).toMatchObject({
      chain_key: 'bitcoin',
      address: ADDRESSES.btcRegtest,
      label: 'Test rig',
    });
    // The form took the success: it cleared, and it says nothing went wrong.
    await waitFor(() => {
      expect(addressInput()).toHaveValue('');
    });
    expect(within(addForm()).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a failed archive leaves the row in place and says why', async () => {
    const { user, fake } = openWalletsPage({ wallets: threeWallets() }, [
      http.delete(WALLET_PATH, () =>
        problem(500, 'Internal Server Error', 'The server encountered an unexpected condition.'),
      ),
    ]);

    const row = await rowFor('Cold storage');
    await user.click(within(row).getByRole('button', { name: ARCHIVE }));
    await user.click(within(row).getByRole('button', { name: CONFIRM_ARCHIVE }));

    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'The server encountered an unexpected condition.',
    );
    await settle();
    // Still listed, still active, and the owner can try again.
    expect(await rowFor('Cold storage')).toBeInTheDocument();
    expect(fake.wallets().find((entry) => entry.id === 1)?.archived).toBe(false);
    expect(within(row).getByRole('button', { name: CONFIRM_ARCHIVE })).toBeEnabled();
  });

  it('a failed archive that never reached the server says so in words', async () => {
    const { user } = openWalletsPage({ wallets: threeWallets() }, [
      http.delete(WALLET_PATH, () => HttpResponse.error()),
    ]);

    const row = await rowFor('Cold storage');
    await user.click(within(row).getByRole('button', { name: ARCHIVE }));
    await user.click(within(row).getByRole('button', { name: CONFIRM_ARCHIVE }));

    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent(/could not archive/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });
});

describe('WalletsPage: add', () => {
  it('adds a wallet and shows it in the list', async () => {
    const { user, fake } = openWalletsPage({ wallets: threeWallets() });
    await walletList();

    await user.selectOptions(chainSelect(), 'kaspa');
    await user.type(addressInput(), ADDRESSES.kasSecondary);
    await user.type(labelInput(), 'Faucet');
    await user.click(submitButton());

    const row = await rowFor('Faucet');
    expect(row).toHaveTextContent('Kaspa');
    expect(shownAddress(row, ADDRESSES.kasSecondary)).toBeInTheDocument();

    expect(creates(fake)).toHaveLength(1);
    expect(creates(fake)[0]?.body).toEqual({
      chain_key: 'kaspa',
      address: ADDRESSES.kasSecondary,
      label: 'Faucet',
    });
    expect(creates(fake)[0]?.contentType).toBe('application/json');
    // Ready for the next one.
    expect(addressInput()).toHaveValue('');
    expect(labelInput()).toHaveValue('');
  });

  it('sends no label rather than an empty one', async () => {
    const { user, fake } = await openEmptyWalletsPage();

    await user.type(addressInput(), ADDRESSES.btcScript);
    await user.type(labelInput(), '   ');
    await user.click(submitButton());

    await waitFor(() => {
      expect(creates(fake)).toHaveLength(1);
    });
    expect(creates(fake)[0]?.body).toMatchObject({ chain_key: 'bitcoin', label: null });
  });

  it('disables the submit button while the request is in flight', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    const release = fake.hold('create');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    // A second press while the first is in flight sends nothing.
    await user.click(submitButton());
    expect(creates(fake)).toHaveLength(1);

    release();

    await waitFor(() => {
      expect(submitButton()).toBeEnabled();
    });
    expect(within(await walletList()).getByTitle(ADDRESSES.btcSegwit)).toBeInTheDocument();
    expect(creates(fake)).toHaveLength(1);
  });

  it('a double click sends one request', async () => {
    // The client half of #5's double-click finding: the button disables
    // itself before a second click can land.
    const { user, fake } = await openEmptyWalletsPage();
    const release = fake.hold('create');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.dblClick(submitButton());
    await settle();

    expect(creates(fake)).toHaveLength(1);
    release();
    await waitFor(() => {
      expect(submitButton()).toBeEnabled();
    });
  });

  it('an empty address is refused without a request', async () => {
    const { user, fake } = await openEmptyWalletsPage();

    await user.click(submitButton());

    expectFieldError(addressInput(), 'An address is required.');

    // Whitespace is empty too.
    await user.type(addressInput(), '   ');
    await user.click(submitButton());
    expectFieldError(addressInput(), 'An address is required.');

    await settle();
    expect(creates(fake)).toHaveLength(0);
  });

  it('clears the local refusal once an address is typed', async () => {
    const { user } = await openEmptyWalletsPage();

    await user.click(submitButton());
    expectFieldError(addressInput(), 'An address is required.');

    await user.type(addressInput(), 't');

    expectNoFieldError(addressInput(), 'An address is required.');
  });
});

describe('WalletsPage: hints', () => {
  it("shows the selected chain's format hint while the address is empty", async () => {
    const { user } = await openEmptyWalletsPage();

    expect(addressInput()).toHaveAccessibleDescription(expect.stringContaining('tb1'));

    await user.selectOptions(chainSelect(), 'kaspa');

    expect(addressInput()).toHaveAccessibleDescription(expect.stringContaining('kaspatest:'));
  });

  it('a hint never disables submission', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    fake.rejectAddress(ADDRESSES.kasPrimary, 'malformed');

    // A Kaspa address with Bitcoin selected: the hint says so...
    await user.type(addressInput(), ADDRESSES.kasPrimary);
    expect(addressInput()).toHaveAccessibleDescription(
      expect.stringMatching(/looks like a kaspa/i),
    );
    // ...and the owner can still send it. The server is the only validator.
    expect(submitButton()).toBeEnabled();
    await user.click(submitButton());

    await waitFor(() => {
      expect(creates(fake)).toHaveLength(1);
    });
    expect(creates(fake)[0]?.body).toMatchObject({
      chain_key: 'bitcoin',
      address: ADDRESSES.kasPrimary,
    });
    // And the server's verdict lands under the field.
    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.malformed);
    });
  });

  it('an extended key is hinted at and still sent', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    const tpub =
      'tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4FyWuejifB9jusQ46QzG87VKp';
    fake.rejectAddress(tpub, 'extended_key');

    await user.type(addressInput(), tpub);

    expect(addressInput()).toHaveAccessibleDescription(
      expect.stringMatching(/only single addresses are supported/i),
    );
    expect(submitButton()).toBeEnabled();
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.extended_key);
    });
    expect(creates(fake)).toHaveLength(1);
  });

  it('the switch-chain control changes the selected chain', async () => {
    const { user, fake } = await openEmptyWalletsPage();

    await user.type(addressInput(), ADDRESSES.kasSecondary);
    await user.click(within(addForm()).getByRole('button', { name: 'Use Kaspa instead' }));

    expect(chainSelect()).toHaveValue('kaspa');
    // The hint has nothing left to say, so the control is gone.
    expect(
      within(addForm()).queryByRole('button', { name: /use .* instead/i }),
    ).not.toBeInTheDocument();
    // The address the owner typed is kept.
    expect(addressInput()).toHaveValue(ADDRESSES.kasSecondary);

    await user.click(submitButton());
    await waitFor(() => {
      expect(creates(fake)).toHaveLength(1);
    });
    expect(creates(fake)[0]?.body).toMatchObject({
      chain_key: 'kaspa',
      address: ADDRESSES.kasSecondary,
    });
  });

  it('the switch-chain control clears a server error and returns focus to the address', async () => {
    // R4 and R5. The 422 was about this address on Bitcoin. Switching to Kaspa
    // makes it a different request, so the old verdict must go - and focus
    // goes back to the field the owner was editing, not onto <body>.
    const { user, fake } = await openEmptyWalletsPage();
    fake.rejectAddress(ADDRESSES.kasSecondary, 'malformed');

    await user.type(addressInput(), ADDRESSES.kasSecondary);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.malformed);
    });

    await user.click(within(addForm()).getByRole('button', { name: 'Use Kaspa instead' }));

    expect(chainSelect()).toHaveValue('kaspa');
    expectNoFieldError(addressInput(), ADDRESS_REJECTIONS.malformed);
    expect(screen.queryByText(ADDRESS_REJECTIONS.malformed)).not.toBeInTheDocument();
    expect(addressInput()).toHaveFocus();
  });

  it('switches back to Bitcoin the same way', async () => {
    const { user } = await openEmptyWalletsPage();

    await user.selectOptions(chainSelect(), 'kaspa');
    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(within(addForm()).getByRole('button', { name: 'Use Bitcoin instead' }));

    expect(chainSelect()).toHaveValue('bitcoin');
  });

  it('the switch-chain control does not submit the form', async () => {
    const { user, fake } = await openEmptyWalletsPage();

    await user.type(addressInput(), ADDRESSES.kasSecondary);
    await user.click(within(addForm()).getByRole('button', { name: 'Use Kaspa instead' }));
    await settle();

    expect(creates(fake)).toHaveLength(0);
  });
});

describe('WalletsPage: field errors', () => {
  it('a 422 on the address renders under the address field', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    fake.rejectAddress(ADDRESSES.btcSegwit, 'bad_checksum');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.type(labelInput(), 'Typo');
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.bad_checksum);
    });
    expectNoFieldError(labelInput(), ADDRESS_REJECTIONS.bad_checksum);
    // Under the field, not also at form level as a generic failure.
    expect(within(addForm()).queryByText(/failed validation/i)).not.toBeInTheDocument();
    // What the owner typed is kept, so they can fix one character.
    expect(addressInput()).toHaveValue(ADDRESSES.btcSegwit);
    expect(labelInput()).toHaveValue('Typo');
  });

  it('a 422 on the label renders under the label field', async () => {
    const { user } = await openEmptyWalletsPage();

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.type(labelInput(), 'x'.repeat(101));
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(labelInput(), LABEL_TOO_LONG.msg);
    });
    expectNoFieldError(addressInput(), LABEL_TOO_LONG.msg);
  });

  it('a 422 on the chain renders under the chain field', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(
      http.post(WALLETS_PATH, () =>
        validationProblem([
          { loc: ['body', 'chain_key'], msg: "Input should be 'bitcoin' or 'kaspa'", type: 'enum' },
        ]),
      ),
    );

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(chainSelect(), "Input should be 'bitcoin' or 'kaspa'");
    });
    expectNoFieldError(addressInput(), "Input should be 'bitcoin' or 'kaspa'");
  });

  it('errors on two fields render under both', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(
      http.post(WALLETS_PATH, () =>
        validationProblem([
          {
            loc: ['body', 'address'],
            msg: ADDRESS_REJECTIONS.wrong_network,
            type: 'wrong_network',
          },
          { loc: ['body', 'label'], ...LABEL_TOO_LONG },
        ]),
      ),
    );

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.wrong_network);
    });
    expectFieldError(labelInput(), LABEL_TOO_LONG.msg);
    // Each message under its own field only.
    expectNotDescribedBy(addressInput(), LABEL_TOO_LONG.msg);
    expectNotDescribedBy(labelInput(), ADDRESS_REJECTIONS.wrong_network);
  });

  it('a 422 at an unknown location renders at form level', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(
      http.post(WALLETS_PATH, () =>
        validationProblem([
          { loc: ['body'], msg: 'Extra inputs are not permitted', type: 'extra_forbidden' },
          {
            loc: ['query', 'dry_run'],
            msg: 'Input should be a valid boolean',
            type: 'bool_parsing',
          },
        ]),
      ),
    );

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    const form = addForm();
    expect(await within(form).findByText('Extra inputs are not permitted')).toHaveAttribute(
      'role',
      'alert',
    );
    expect(within(form).getByText('Input should be a valid boolean')).toBeInTheDocument();
    // Not pinned on a field it has nothing to do with.
    for (const input of [addressInput(), labelInput(), chainSelect()]) {
      expectNoFieldError(input, 'Extra inputs are not permitted');
      expect(input).not.toHaveAttribute('aria-invalid', 'true');
    }
  });

  it('a 422 whose errors cannot be read still says the request was refused', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(
      http.post(WALLETS_PATH, () =>
        validationProblem([{ loc: 'body', msg: 7, type: 'x' }] as never),
      ),
    );

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    expect(await within(addForm()).findByRole('alert')).toHaveTextContent(
      'The request parameters failed validation.',
    );
  });

  it.each([
    ['active', false, DUPLICATE_DETAIL],
    ['archived', true, DUPLICATE_ARCHIVED_DETAIL],
  ])('a 409 renders under the address field (%s duplicate)', async (_state, archived, detail) => {
    const { user } = openWalletsPage({
      wallets: [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'Existing', archived })],
    });
    await screen.findByRole('region', { name: 'Your wallets' });

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    await waitFor(() => {
      expectFieldError(addressInput(), detail);
    });
    // A duplicate is not a failure of the page.
    expect(within(addForm()).getAllByRole('alert')).toHaveLength(1);
  });

  it('a server failure on add renders at form level with the server sentence', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(
      http.post(WALLETS_PATH, () =>
        problem(500, 'Internal Server Error', 'The server encountered an unexpected condition.'),
      ),
    );

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    expect(await within(addForm()).findByRole('alert')).toHaveTextContent(
      'The server encountered an unexpected condition.',
    );
    expect(addressInput()).not.toHaveAttribute('aria-invalid', 'true');
    expect(submitButton()).toBeEnabled();
  });

  it('an add that never reached the server says so and keeps the input', async () => {
    const { user } = await openEmptyWalletsPage();
    server.use(http.post(WALLETS_PATH, () => HttpResponse.error()));

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());

    const alert = await within(addForm()).findByRole('alert');
    expect(alert).toHaveTextContent(/could not add the wallet/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
    expect(addressInput()).toHaveValue(ADDRESSES.btcSegwit);
  });

  it('a server error under the address clears once the address is edited', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    fake.rejectAddress(ADDRESSES.btcSegwit, 'bad_checksum');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.bad_checksum);
    });

    // Fixing the typo: the old verdict is about a value no longer in the box.
    await user.type(addressInput(), '{Backspace}');

    expectNoFieldError(addressInput(), ADDRESS_REJECTIONS.bad_checksum);
    expect(screen.queryByText(ADDRESS_REJECTIONS.bad_checksum)).not.toBeInTheDocument();
  });

  it('a duplicate refusal clears once the address is edited', async () => {
    const { user } = openWalletsPage({
      wallets: [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'Existing' })],
    });
    await rowFor('Existing');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), DUPLICATE_DETAIL);
    });

    await user.clear(addressInput());
    await user.type(addressInput(), ADDRESSES.btcLegacy);

    expectNoFieldError(addressInput(), DUPLICATE_DETAIL);
  });

  it('a server error under the address clears once the chain is changed', async () => {
    // Witness for M5 (the reset on a chain-select change). The duplicate is a
    // fact about this address on Bitcoin; on Kaspa it is a different question
    // the server has not answered yet.
    const { user } = openWalletsPage({
      wallets: [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'Existing' })],
    });
    await rowFor('Existing');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), DUPLICATE_DETAIL);
    });

    await user.selectOptions(chainSelect(), 'kaspa');

    expectNoFieldError(addressInput(), DUPLICATE_DETAIL);
    expect(screen.queryByText(DUPLICATE_DETAIL)).not.toBeInTheDocument();
  });

  it('a server error under the label clears once the label is edited', async () => {
    const { user } = await openEmptyWalletsPage();

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.type(labelInput(), 'x'.repeat(101));
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(labelInput(), LABEL_TOO_LONG.msg);
    });

    await user.type(labelInput(), '{Backspace}');

    expectNoFieldError(labelInput(), LABEL_TOO_LONG.msg);
  });

  it('a field error clears once the resubmission succeeds', async () => {
    const { user, fake } = await openEmptyWalletsPage();
    fake.rejectAddress(ADDRESSES.btcSegwit, 'bad_checksum');

    await user.type(addressInput(), ADDRESSES.btcSegwit);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), ADDRESS_REJECTIONS.bad_checksum);
    });

    // The fake refuses an address once; the second attempt goes through.
    await user.click(submitButton());

    await waitFor(() => {
      expect(fake.wallets()).toHaveLength(1);
    });
    await waitFor(() => {
      expectNoFieldError(addressInput(), ADDRESS_REJECTIONS.bad_checksum);
    });
  });
});

describe('WalletsPage: archive and restore', () => {
  it('archiving asks for confirmation first', async () => {
    const { user, fake } = openWalletsPage({ wallets: threeWallets() });

    const row = await rowFor('Cold storage');
    await user.click(within(row).getByRole('button', { name: ARCHIVE }));

    // The consequence, in words, before anything is sent.
    expect(row).toHaveTextContent(/stop being read/i);
    expect(row).toHaveTextContent(/leave the total/i);
    expect(within(row).getByRole('button', { name: CONFIRM_ARCHIVE })).toBeInTheDocument();
    await settle();
    expect(fake.writes('DELETE', '/api/wallets/1')).toHaveLength(0);
    expect(fake.requests.filter((entry) => entry.method !== 'GET')).toHaveLength(0);

    // Cancel sends nothing and puts the row back.
    await user.click(within(row).getByRole('button', { name: CANCEL_ARCHIVE }));
    expect(within(row).getByRole('button', { name: ARCHIVE })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: CONFIRM_ARCHIVE })).not.toBeInTheDocument();
    await settle();
    expect(fake.requests.filter((entry) => entry.method !== 'GET')).toHaveLength(0);
  });

  it('moves focus to Confirm, and back to Archive on Cancel', async () => {
    // R5. The pressed button disappears in both steps; focus left on <body>
    // sends a keyboard user back to the top of the page.
    const { user } = openWalletsPage({ wallets: threeWallets() });
    await walletList();

    await user.click(screen.getByRole('button', { name: 'Archive Cold storage' }));
    expect(screen.getByRole('button', { name: 'Confirm archive of Cold storage' })).toHaveFocus();

    await user.click(screen.getByRole('button', { name: 'Cancel archiving Cold storage' }));
    expect(screen.getByRole('button', { name: 'Archive Cold storage' })).toHaveFocus();
  });

  it('moves focus the same way from the keyboard', async () => {
    const { user } = openWalletsPage({ wallets: threeWallets() });
    await walletList();

    screen.getByRole('button', { name: 'Archive Mining payouts' }).focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Confirm archive of Mining payouts' })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole('button', { name: 'Cancel archiving Mining payouts' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Archive Mining payouts' })).toHaveFocus();
  });

  it('moves focus to the list heading once an archived row leaves the list', async () => {
    const { user } = openWalletsPage({ wallets: threeWallets() });
    await walletList();

    await user.click(screen.getByRole('button', { name: 'Archive Cold storage' }));
    await user.click(screen.getByRole('button', { name: 'Confirm archive of Cold storage' }));

    await waitFor(() => {
      expect(screen.queryByText('Cold storage')).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Your wallets' })).toHaveFocus();
    });
  });

  it('moves focus to Restore when an archived row stays in the list', async () => {
    // With archived wallets shown, the row stays and swaps Archive for Restore.
    const { user } = openWalletsPage({ wallets: threeWallets() });
    await walletList();
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));

    await user.click(await screen.findByRole('button', { name: 'Archive Cold storage' }));
    await user.click(screen.getByRole('button', { name: 'Confirm archive of Cold storage' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Restore Cold storage' })).toHaveFocus();
    });
  });

  it('an archive the server did not apply does not steal focus on a later list change', async () => {
    // The server answers 204 but the list does not change: another session
    // restored the wallet in between, or the request was a repeat. The
    // refetched list is structurally equal, so the pending focus hand-off never
    // fires. It must not fire later either: toggling "Show archived" is the
    // owner's own action, and focus belongs on the control they just used.
    const { user } = openWalletsPage({ wallets: threeWallets() }, [
      http.delete(WALLET_PATH, () => new HttpResponse(null, { status: 204 })),
    ]);
    await walletList();

    await user.click(screen.getByRole('button', { name: 'Archive Cold storage' }));
    await user.click(screen.getByRole('button', { name: 'Confirm archive of Cold storage' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Archive Cold storage' })).toBeVisible();
    });
    await settle();

    const toggle = screen.getByRole('checkbox', { name: 'Show archived' });
    await user.click(toggle);
    await screen.findByRole('button', { name: 'Archive Cold storage' });
    await settle();

    expect(toggle).toHaveFocus();
  });

  describe('when the session dies between an archive and its refetch', () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it('lands on the login page with nothing thrown', async () => {
      // The archive is answered 204; the refetch it triggers finds the session
      // gone and is answered 401, which purges every cached query - including
      // the wallet list the focus hand-off reads to decide where focus goes.
      // That read comes back empty; it must degrade, not throw, and the owner
      // must end up on the sign-in form.
      const consoleErrors = vi.spyOn(console, 'error');
      const user = userEvent.setup();
      const session = fakeSession({ initialUser: TEST_USERNAME });
      const fake = fakePortfolio({ wallets: threeWallets(), session });
      server.use(...session.handlers, ...fake.handlers);
      server.use(
        http.delete(WALLET_PATH, () => {
          session.signOut();
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderApp(['/wallets']);
      await walletList();

      await user.click(screen.getByRole('button', { name: 'Archive Cold storage' }));
      await user.click(screen.getByRole('button', { name: 'Confirm archive of Cold storage' }));

      expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
      await settle();
      expect(currentPath()).toBe('/login');
      // The refetch really was refused, so this is the path under test.
      expect(
        fake.requests.some(
          (entry) => entry.method === 'GET' && new URL(entry.url).pathname === WALLETS_PATH,
        ),
      ).toBe(true);
      expect(consoleErrors).not.toHaveBeenCalled();
    });
  });

  it('archive then restore with archived shown brings back Archive, not the confirm step', async () => {
    // Witness for M10: without `setConfirming(false)` after a successful
    // archive, the row keeps its confirm state through the archived phase and
    // comes back from a restore already armed.
    const { user, fake } = openWalletsPage({ wallets: threeWallets() });
    await walletList();
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));

    await user.click(await screen.findByRole('button', { name: 'Archive Cold storage' }));
    await user.click(screen.getByRole('button', { name: 'Confirm archive of Cold storage' }));
    await user.click(await screen.findByRole('button', { name: 'Restore Cold storage' }));

    await waitFor(() => {
      expect(fake.wallets().find((entry) => entry.id === 1)?.archived).toBe(false);
    });
    expect(await screen.findByRole('button', { name: 'Archive Cold storage' })).toBeVisible();
    expect(
      screen.queryByRole('button', { name: 'Confirm archive of Cold storage' }),
    ).not.toBeInTheDocument();
    expect(await rowFor('Cold storage')).not.toHaveTextContent(/stop being read/i);
  });

  it('confirming one row does not arm the others', async () => {
    const { user } = openWalletsPage({ wallets: threeWallets() });

    const cold = await rowFor('Cold storage');
    await user.click(within(cold).getByRole('button', { name: ARCHIVE }));

    const mining = await rowFor('Mining payouts');
    expect(within(mining).queryByRole('button', { name: CONFIRM_ARCHIVE })).not.toBeInTheDocument();
  });

  it('archiving removes the wallet from the active list', async () => {
    const { user, fake } = openWalletsPage({ wallets: threeWallets() });

    const row = await rowFor('Cold storage');
    await user.click(within(row).getByRole('button', { name: ARCHIVE }));
    await user.click(within(row).getByRole('button', { name: CONFIRM_ARCHIVE }));

    await waitFor(() => {
      expect(screen.queryByText('Cold storage')).not.toBeInTheDocument();
    });
    const list = await walletList();
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);

    // By id, as a DELETE, carrying the header the write guard requires.
    const deletes = fake.writes('DELETE', '/api/wallets/1');
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.contentType).toBe('application/json');
    expect(fake.wallets().find((entry) => entry.id === 1)?.archived).toBe(true);
  });

  it('archiving the last wallet leaves the empty state', async () => {
    const { user } = openWalletsPage({
      wallets: [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'Only one' })],
    });

    const row = await rowFor('Only one');
    await user.click(within(row).getByRole('button', { name: ARCHIVE }));
    await user.click(within(row).getByRole('button', { name: CONFIRM_ARCHIVE }));

    expect(await screen.findByRole('heading', { name: /no wallets yet/i })).toBeInTheDocument();
  });

  it('show archived lists archived wallets with a restore button', async () => {
    const wallets = [
      ...threeWallets(),
      wallet({
        id: 4,
        chain_key: 'bitcoin',
        address: ADDRESSES.btcScript,
        label: 'Old exchange',
        archived: true,
      }),
    ];
    const { user, fake } = openWalletsPage({ wallets });

    await rowFor('Cold storage');
    expect(screen.queryByText('Old exchange')).not.toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));

    const archived = await rowFor('Old exchange');
    // Marked in text, not by colour alone.
    expect(within(archived).getByText('Archived')).toBeInTheDocument();
    expect(within(archived).getByRole('button', { name: RESTORE })).toBeInTheDocument();
    expect(within(archived).queryByRole('button', { name: ARCHIVE })).not.toBeInTheDocument();

    // The active ones are still there, unmarked, with no restore.
    const cold = await rowFor('Cold storage');
    expect(within(cold).queryByText('Archived')).not.toBeInTheDocument();
    expect(within(cold).queryByRole('button', { name: RESTORE })).not.toBeInTheDocument();

    expect(
      fake.requests.some(
        (entry) => new URL(entry.url).searchParams.get('include_archived') === 'true',
      ),
    ).toBe(true);
  });

  it('restoring returns the wallet to the active list', async () => {
    const wallets = [
      ...threeWallets(),
      wallet({
        id: 4,
        chain_key: 'bitcoin',
        address: ADDRESSES.btcScript,
        label: 'Old exchange',
        archived: true,
      }),
    ];
    const { user, fake } = openWalletsPage({ wallets });

    await rowFor('Cold storage');
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    const archived = await rowFor('Old exchange');

    await user.click(within(archived).getByRole('button', { name: RESTORE }));

    await waitFor(() => {
      expect(fake.wallets().find((entry) => entry.id === 4)?.archived).toBe(false);
    });
    const patches = fake.writes('PATCH', '/api/wallets/4');
    expect(patches).toHaveLength(1);
    // Only `archived`: sending the label along would be a rename nobody asked for.
    expect(patches[0]?.body).toEqual({ archived: false });

    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));

    const restored = await rowFor('Old exchange');
    expect(within(restored).getByRole('button', { name: ARCHIVE })).toBeInTheDocument();
    expect(within(restored).queryByText('Archived')).not.toBeInTheDocument();
  });

  it('a failed restore says why and keeps the wallet archived', async () => {
    const { user, fake } = openWalletsPage({
      wallets: [
        wallet({ id: 4, address: ADDRESSES.btcScript, label: 'Old exchange', archived: true }),
      ],
    });
    server.use(http.patch(WALLET_PATH, () => problem(404, 'Not Found', WALLET_NOT_FOUND_DETAIL)));

    await screen.findByRole('heading', { name: /no wallets yet/i });
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    const archived = await rowFor('Old exchange');
    await user.click(within(archived).getByRole('button', { name: RESTORE }));

    expect(await within(archived).findByRole('alert')).toHaveTextContent(WALLET_NOT_FOUND_DETAIL);
    expect(fake.wallets()[0]?.archived).toBe(true);
    expect(within(archived).getByText('Archived')).toBeInTheDocument();
  });

  it('the archived duplicate sentence and the restore control are on one page', async () => {
    // The 409 tells the owner to restore; the page has to make that possible
    // without a trip to curl.
    const { user, fake } = openWalletsPage({
      wallets: [
        wallet({ id: 4, address: ADDRESSES.btcScript, label: 'Old exchange', archived: true }),
      ],
    });
    await screen.findByRole('heading', { name: /no wallets yet/i });

    await user.type(addressInput(), ADDRESSES.btcScript);
    await user.click(submitButton());
    await waitFor(() => {
      expectFieldError(addressInput(), DUPLICATE_ARCHIVED_DETAIL);
    });

    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    await user.click(within(await rowFor('Old exchange')).getByRole('button', { name: RESTORE }));

    await waitFor(() => {
      expect(fake.wallets()[0]?.archived).toBe(false);
    });
  });
});
