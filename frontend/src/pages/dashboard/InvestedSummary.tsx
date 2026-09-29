import type { Position, PositionTotals } from '@/api/accounting';
import { Money } from '@/components/Money';
import {
  AMOUNT_FORMAT,
  EXCLUSION_REASON_MESSAGES,
  groupExclusions,
  SIGNED_FORMAT,
} from '@/lib/accounting';
import { isZeroMoney, money } from '@/lib/money';
import { ReturnPercent } from '@/pages/dashboard/ReturnPercent';

interface InvestedSummaryProps {
  readonly totals: PositionTotals;
  /** The held positions: what "every held position is excluded" is judged against. */
  readonly held: readonly Position[];
  readonly quoteCurrency: string;
  readonly unallocatedCosts: string;
}

/**
 * Invested against market value, unrealized P&L with its return, and realized P&L - every
 * figure straight from `totals`, summed by the backend. The page adds nothing up: a second
 * sum here would give it two sources for one number, the same reason `TotalSummary` renders
 * the balances' `total` as sent.
 *
 * The three figures the exclusions bear on show "—" when **every** held position is left
 * out. `totals` is then a sum over nothing, and "0.00 invested" beside a table of holdings
 * that cost something is the fabricated zero this page exists to refuse. Realized P&L is
 * summed over every position, so it is a real figure in that case and keeps showing.
 */
export function InvestedSummary({
  totals,
  held,
  quoteCurrency,
  unallocatedCosts,
}: InvestedSummaryProps) {
  const excludedAssets = new Set(totals.excluded.map((entry) => entry.asset));
  const nothingComparable =
    held.length > 0 && held.every((position) => excludedAssets.has(position.asset));

  return (
    <>
      <dl className="invested-summary">
        <div>
          <dt>Invested</dt>
          <dd>
            {nothingComparable ? (
              '—'
            ) : (
              <>
                <Money value={money(totals.total_invested)} options={AMOUNT_FORMAT} />{' '}
                {quoteCurrency}
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Market value</dt>
          <dd>
            {nothingComparable ? (
              '—'
            ) : (
              <>
                <Money value={money(totals.market_value)} options={AMOUNT_FORMAT} /> {quoteCurrency}
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Unrealized P&amp;L</dt>
          <dd>
            {nothingComparable ? (
              '—'
            ) : (
              <>
                <Money value={money(totals.unrealized_pnl)} options={SIGNED_FORMAT} />{' '}
                {quoteCurrency}
                <span className="pnl-return">
                  Return <ReturnPercent value={totals.unrealized_return_pct} />
                </span>
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Realized P&amp;L</dt>
          <dd>
            <Money value={money(totals.realized_pnl)} options={SIGNED_FORMAT} /> {quoteCurrency}
          </dd>
        </div>
      </dl>

      {totals.excluded.length > 0 && (
        <>
          <p>Left out of these totals:</p>
          <ul className="excluded-list">
            {groupExclusions(totals.excluded).map(({ reason, assets }) => (
              <li key={reason}>
                <strong>{assets.join(', ')}</strong>: {EXCLUSION_REASON_MESSAGES[reason]}
              </li>
            ))}
          </ul>
          <p>Realized P&amp;L covers every position, including these.</p>
        </>
      )}

      {!isZeroMoney(money(unallocatedCosts)) && (
        <p>
          Fees not assigned to any asset:{' '}
          <Money value={money(unallocatedCosts)} options={AMOUNT_FORMAT} /> {quoteCurrency}{' '}
          (conversions between stablecoins).
        </p>
      )}
    </>
  );
}
