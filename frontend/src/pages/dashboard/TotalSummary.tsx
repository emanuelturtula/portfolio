import type { CurrentBalances } from '@/api/balances';
import { Money } from '@/components/Money';
import { money } from '@/lib/money';

const FIAT_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

interface TotalSummaryProps {
  readonly data: CurrentBalances;
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
 * The total, marked partial whenever the backend says it is, and naming what it is
 * missing rather than leaving "partial" to speak for itself.
 *
 * `total` is rendered as the backend sent it and never recomputed - see the spec's "Asset
 * rows are sums of the wallet rows the response already carries" for why a second sum here
 * would give the page two disagreeing sources for one number.
 */
export function TotalSummary({ data }: TotalSummaryProps) {
  const anyStalePrice = data.wallets.some((wallet) => wallet.price?.stale === true);

  return (
    <section aria-labelledby="total-heading">
      <h2 id="total-heading">Total value</h2>
      <p className="total-amount">
        <Money value={money(data.total)} options={FIAT_OPTIONS} /> {data.quote_currency}
        {!data.complete && ' - Partial'}
      </p>
      {!data.complete && <p>This total does not include {describeMissing(data)}.</p>}
      {anyStalePrice && <p>This total includes at least one stale price.</p>}
    </section>
  );
}
