import type { CurrentBalances, WalletBalance } from '@/api/balances';
import type { Wallet } from '@/api/wallets';
import { Address } from '@/components/Address';
import { Money } from '@/components/Money';
import { RelativeTime } from '@/components/RelativeTime';
import { chainDisplayName } from '@/lib/chains';
import {
  assessFreshness,
  freshnessMessage,
  SYNC_ERROR_MESSAGES,
  UNKNOWN_FAILURE_MESSAGE,
  type SyncRunSummary,
} from '@/lib/freshness';
import { fromBaseUnits, money } from '@/lib/money';

const FIAT_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

/** Why an unread wallet's chain has no reading yet, when the settled run says so. */
function unreadReason(
  chainKey: string,
  settledRun: SyncRunSummary | undefined,
  freshnessKnown: boolean,
): string | undefined {
  if (!freshnessKnown || settledRun === undefined) {
    return undefined;
  }

  const outcome = settledRun.chains.find((chain) => chain.chain_key === chainKey);
  if (outcome?.status !== 'failed') {
    return undefined;
  }

  return outcome.error_kind == null
    ? UNKNOWN_FAILURE_MESSAGE
    : SYNC_ERROR_MESSAGES[outcome.error_kind];
}

/**
 * The signed pending amount after the quantity, e.g. "(+0.00012 BTC pending)". `null` when
 * there is nothing to show: no pending, a zero pending, or a wallet with no decimals yet.
 *
 * Rendered through `<Money>`, not a hand-formatted string, so it carries its own exact
 * `<data value>` - AC8 ("amounts render from strings at full precision") applies to every
 * amount on the page, not only to the quantity next to it. The sign is kept outside the
 * `<Money>` element and applied to the unsigned magnitude, so the two - the explicit `+`
 * this row wants, and `formatMoney`'s own `-` for a negative amount - never disagree.
 */
function renderPending(wallet: WalletBalance) {
  if (wallet.pending === null || wallet.pending === '0' || wallet.decimals === null) {
    return null;
  }

  const amount = fromBaseUnits(wallet.pending, wallet.decimals);
  const negative = amount.startsWith('-');
  const magnitude = money(negative ? amount.slice(1) : amount);

  return (
    <span className="pending">
      {' '}
      ({negative ? '-' : '+'}
      <Money value={magnitude} /> {wallet.asset_symbol} pending)
    </span>
  );
}

/**
 * The Freshness cell's content for one read wallet (`reading !== undefined`).
 *
 * A standalone function, rather than a ternary inline in the JSX, so that the type of
 * `assessFreshness`'s result narrows correctly: it is computed here exactly once, in the
 * one branch that needs it, instead of being assigned earlier under a condition that
 * `freshnessKnown`/`reading` together already guarantee - which left a `freshness !==
 * undefined` check with an unreachable `else` that nothing could ever exercise.
 */
function renderFreshnessCell(
  reading: { readonly observedAt: string },
  freshnessKnown: boolean,
  settledRun: SyncRunSummary | undefined,
  chainKey: string,
) {
  if (!freshnessKnown) {
    return (
      <>
        Read <RelativeTime value={reading.observedAt} />
      </>
    );
  }

  const freshness = assessFreshness(settledRun, chainKey, reading.observedAt);
  if (freshness.status === 'fresh') {
    return 'Up to date';
  }

  return (
    <>
      {freshnessMessage(freshness)} - showing the balance from{' '}
      <RelativeTime value={reading.observedAt} />
    </>
  );
}

interface WalletRowProps {
  readonly wallet: WalletBalance;
  readonly walletRecord: Wallet | undefined;
  readonly quoteCurrency: string;
  readonly settledRun: SyncRunSummary | undefined;
  readonly freshnessKnown: boolean;
}

function WalletBalanceRow({
  wallet,
  walletRecord,
  quoteCurrency,
  settledRun,
  freshnessKnown,
}: WalletRowProps) {
  // Bundled into one object so a null check on both fields narrows both at once: a boolean
  // derived from them (`const unread = a === null || b === null`) does not carry that
  // narrowing back to `wallet.quantity`/`wallet.observed_at` at the call sites below.
  const reading =
    wallet.observed_at !== null && wallet.quantity !== null
      ? { observedAt: wallet.observed_at, quantity: wallet.quantity }
      : undefined;
  const heading =
    wallet.label ??
    (walletRecord === undefined
      ? `${chainDisplayName(wallet.chain_key)} wallet #${String(wallet.wallet_id)}`
      : undefined);
  const reasonForUnread =
    reading === undefined ? unreadReason(wallet.chain_key, settledRun, freshnessKnown) : undefined;

  return (
    <tr>
      <td>
        <div className="wallet-chain">{chainDisplayName(wallet.chain_key)}</div>
        {heading !== undefined && <div className="wallet-name">{heading}</div>}
        {walletRecord !== undefined && <Address value={walletRecord.address} />}
      </td>
      <td>
        {reading === undefined ? (
          <>
            Not read yet
            {reasonForUnread !== undefined && <> - {reasonForUnread}</>}
          </>
        ) : (
          <>
            <Money value={money(reading.quantity)} /> {wallet.asset_symbol}
            {renderPending(wallet)}
          </>
        )}
      </td>
      <td>
        {reading === undefined
          ? '—'
          : renderFreshnessCell(reading, freshnessKnown, settledRun, wallet.chain_key)}
      </td>
      <td>
        {wallet.value === null ? (
          '—'
        ) : (
          <>
            <Money value={money(wallet.value)} options={FIAT_OPTIONS} /> {quoteCurrency}
            {wallet.price?.stale === true && (
              <>
                {' '}
                (stale, as of <RelativeTime value={wallet.price.as_of} />)
              </>
            )}
          </>
        )}
      </td>
    </tr>
  );
}

interface WalletBalanceTableProps {
  readonly data: CurrentBalances;
  readonly walletsById: ReadonlyMap<number, Wallet>;
  readonly settledRun: SyncRunSummary | undefined;
  /** Whether the runs query succeeded, so a row can be judged against `settledRun` at all. */
  readonly freshnessKnown: boolean;
}

/** One row per wallet: its reading, its freshness, and its value. */
export function WalletBalanceTable({
  data,
  walletsById,
  settledRun,
  freshnessKnown,
}: WalletBalanceTableProps) {
  return (
    <section aria-labelledby="wallet-balances-heading">
      <h2 id="wallet-balances-heading">Wallets</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">Wallet</th>
            <th scope="col">Quantity</th>
            <th scope="col">Freshness</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {data.wallets.map((wallet) => (
            <WalletBalanceRow
              key={wallet.wallet_id}
              wallet={wallet}
              walletRecord={walletsById.get(wallet.wallet_id)}
              quoteCurrency={data.quote_currency}
              settledRun={settledRun}
              freshnessKnown={freshnessKnown}
            />
          ))}
        </tbody>
      </table>
    </section>
  );
}
