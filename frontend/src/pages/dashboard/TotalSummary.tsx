import type { CurrentBalances } from '@/api/balances';
import { Money } from '@/components/Money';
import { assessFreshness, type SyncRunSummary } from '@/lib/freshness';
import { money } from '@/lib/money';

const FIAT_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

interface TotalSummaryProps {
  readonly data: CurrentBalances;
  readonly settledRun: SyncRunSummary | undefined;
  /** Whether the runs query succeeded, so a row can be judged against `settledRun` at all. */
  readonly freshnessKnown: boolean;
}

/**
 * What `unread` and `unpriced` add up to, in one sentence - never just "partial" on its
 * own, which would say something is missing without saying what.
 */
function describeMissing(data: CurrentBalances): string {
  const parts: string[] = [];

  if (data.unread.length > 0) {
    const count = data.unread.length;
    parts.push(`${String(count)} wallet${count === 1 ? '' : 's'} not yet read`);
  }

  if (data.unpriced.length > 0) {
    const symbols = data.unpriced.map((holding) => holding.asset_symbol).join(', ');
    parts.push(`prices for ${symbols}`);
  }

  return parts.join(' and ');
}

/**
 * How many wallet rows carry a value that the settled run did not refresh - a reading that
 * is not fresh under {@link assessFreshness}, but still counts toward `total` because the
 * backend sums every row that has a value regardless of its age.
 *
 * The backend's own `complete` does not capture this: `complete` is about `unread` and
 * `unpriced` only, and a wallet with a stale-but-present reading is in neither list, yet
 * `total` still carries a number that is not current - see the spec's "Never a silent
 * zero" row for a read wallet whose chain failed.
 *
 * `undefined` when freshness could not be determined (the runs query failed), so the
 * caller says nothing here rather than guessing - the page-level notice already covers
 * that failure.
 */
function countUnrefreshed(
  data: CurrentBalances,
  settledRun: SyncRunSummary | undefined,
  freshnessKnown: boolean,
): number | undefined {
  if (!freshnessKnown) {
    return undefined;
  }

  return data.wallets.filter((wallet) => {
    if (wallet.value === null || wallet.observed_at === null) {
      return false;
    }
    return assessFreshness(settledRun, wallet.chain_key, wallet.observed_at).status !== 'fresh';
  }).length;
}

/**
 * The total, marked partial whenever the backend says it is, and naming what it is
 * missing rather than leaving "partial" to speak for itself.
 *
 * `total` is rendered as the backend sent it and never recomputed - see the spec's "Asset
 * rows are sums of the wallet rows the response already carries" for why a second sum here
 * would give the page two disagreeing sources for one number. The one exception is the
 * headline itself: when the total is partial *and* nothing could be valued at all, `total`
 * is an empty sum rather than a real amount, and presenting `0.00` there would be exactly
 * the fabricated zero this page exists to refuse. A portfolio that is genuinely worth zero
 * still has `complete: true` and still renders `0.00`.
 */
export function TotalSummary({ data, settledRun, freshnessKnown }: TotalSummaryProps) {
  const anyStalePrice = data.wallets.some((wallet) => wallet.price?.stale === true);
  const anyValued = data.wallets.some((wallet) => wallet.value !== null);
  const unrefreshedCount = countUnrefreshed(data, settledRun, freshnessKnown);

  return (
    <section aria-labelledby="total-heading">
      <h2 id="total-heading">Total value</h2>
      <p className="total-amount">
        {!data.complete && !anyValued ? (
          'Not available yet'
        ) : (
          <>
            <Money value={money(data.total)} options={FIAT_OPTIONS} /> {data.quote_currency}
          </>
        )}
        {!data.complete && ' - Partial'}
      </p>
      {!data.complete && <p>This total does not include {describeMissing(data)}.</p>}
      {unrefreshedCount !== undefined && unrefreshedCount > 0 && (
        <p>
          This total includes {unrefreshedCount} balance{unrefreshedCount === 1 ? '' : 's'} the last
          sync could not refresh.
        </p>
      )}
      {anyStalePrice && <p>This total includes at least one stale price.</p>}
    </section>
  );
}
