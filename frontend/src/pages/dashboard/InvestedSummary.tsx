import type { Position, PositionTotals } from '@/api/accounting';
import { Money } from '@/components/Money';
import {
  AMOUNT_FORMAT,
  assetsWithUnreliableRealizedPnl,
  EXCLUSION_REASON_MESSAGES,
  formatList,
  groupExclusions,
  hasUnmatchedProceeds,
  isHeld,
  SIGNED_FORMAT,
  UNMATCHED_PROCEEDS_EXPLANATION,
  UNMATCHED_PROCEEDS_LABEL,
} from '@/lib/accounting';
import { isZeroMoney, money, toneOf } from '@/lib/money';
import { ReturnPercent } from '@/pages/dashboard/ReturnPercent';

interface InvestedSummaryProps {
  readonly totals: PositionTotals;
  /**
   * Every position, held or not. The held ones are what "every held position is excluded" and
   * the stale-price sentence are judged against; all of them are what the realized P&L caveat
   * is, since realized P&L is summed over the closed ones too.
   */
  readonly positions: readonly Position[];
  readonly quoteCurrency: string;
  readonly unallocatedCosts: string;
}

/** The class that colours a P&L tile by its sign: the sign in the figure says it first. */
function toneClass(pnl: string): string {
  return `kpi-${toneOf(money(pnl))}`;
}

/**
 * Invested against market value, unrealized P&L with its return, realized P&L and, when
 * a position carries any, the unmatched proceeds: what sales brought in for units with no
 * known cost. Every figure comes straight from `totals`, summed by the backend. The page
 * adds nothing up: a second sum here would give it two sources for one number, the same
 * reason `TotalSummary` renders the balances' `total` as sent. The amounts in the list under
 * the summary are each position's own, as sent, for the same reason.
 *
 * The three figures the exclusions bear on show "—" when **every** held position is left
 * out. `totals` is then a sum over nothing, and "0.00 invested" beside a table of holdings
 * that cost something is the fabricated zero this page exists to refuse. Realized P&L is
 * summed over every position, so it is a real figure in that case and keeps showing. When
 * **nothing** is held the totals are a genuine zero and are shown as one (spec 022, R7).
 *
 * Unmatched proceeds are summed over every position too, held or not, excluded or not, so
 * they keep showing in that case as well. They show when **at least one position carries a
 * non-zero figure**, and not when the total is non-zero: the figure is signed, so two
 * positions can cancel to a total of exactly zero, and a rule on the total would then mark a
 * closed position in the line below while hiding the figure and the explanation that mark
 * refers to. Judged on the positions, the figure, its explanation, its list and the mark
 * always appear together (spec 026). A non-zero total with no position carrying it is a
 * response the backend cannot write, so nothing handles it (spec 022, R1).
 *
 * Like `TotalSummary`, it says when a price it used is stale - a total is only as current
 * as the prices under it - and, unlike it, when the history under a realized figure is
 * known to be short.
 */
export function InvestedSummary({
  totals,
  positions,
  quoteCurrency,
  unallocatedCosts,
}: InvestedSummaryProps) {
  const held = positions.filter(isHeld);
  const excludedAssets = new Set(totals.excluded.map((entry) => entry.asset));
  const nothingComparable =
    held.length > 0 && held.every((position) => excludedAssets.has(position.asset));
  // Only the positions the totals are made of: an excluded position's stale price is not in
  // any figure above, and saying so would blame the totals for a number they do not use.
  const anyStalePrice = held.some(
    (position) => position.price?.stale === true && !excludedAssets.has(position.asset),
  );
  const unreliableRealized = assetsWithUnreliableRealizedPnl(positions);
  const unmatched = positions.filter(hasUnmatchedProceeds);

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
                <span className="kpi-unit">{quoteCurrency}</span>
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
                <Money value={money(totals.market_value)} options={AMOUNT_FORMAT} />{' '}
                <span className="kpi-unit">{quoteCurrency}</span>
              </>
            )}
          </dd>
        </div>
        <div className={nothingComparable ? undefined : toneClass(totals.unrealized_pnl)}>
          <dt>Unrealized P&amp;L</dt>
          <dd>
            {nothingComparable ? (
              '—'
            ) : (
              <>
                <Money value={money(totals.unrealized_pnl)} options={SIGNED_FORMAT} />{' '}
                <span className="kpi-unit">{quoteCurrency}</span>
                <span className="pnl-return">
                  Return <ReturnPercent value={totals.unrealized_return_pct} />
                </span>
              </>
            )}
          </dd>
        </div>
        <div className={toneClass(totals.realized_pnl)}>
          <dt>Realized P&amp;L</dt>
          <dd>
            <Money value={money(totals.realized_pnl)} options={SIGNED_FORMAT} />{' '}
            <span className="kpi-unit">{quoteCurrency}</span>
          </dd>
        </div>
        {unmatched.length > 0 && (
          <div>
            <dt>{UNMATCHED_PROCEEDS_LABEL}</dt>
            <dd>
              {/* An amount that came in, not a gain or a loss: no `+`. A negative one keeps its minus. */}
              <Money value={money(totals.unmatched_proceeds)} options={AMOUNT_FORMAT} />{' '}
              <span className="kpi-unit">{quoteCurrency}</span>
            </dd>
          </div>
        )}
      </dl>

      <div className="invested-notes">
        {anyStalePrice && <p>These totals include at least one stale price.</p>}
        {unreliableRealized.length > 0 && (
          <p>
            Realized P&amp;L may be inaccurate for {formatList(unreliableRealized)}: the imported
            history is incomplete, or a fee could not be valued.
          </p>
        )}

        {unmatched.length > 0 && (
          <>
            <p>{UNMATCHED_PROCEEDS_EXPLANATION}</p>
            <ul className="excluded-list">
              {unmatched.map((position) => (
                <li key={position.asset}>
                  <strong>{position.asset}</strong>:{' '}
                  <Money value={money(position.unmatched_proceeds)} options={AMOUNT_FORMAT} />{' '}
                  {quoteCurrency}
                </li>
              ))}
            </ul>
          </>
        )}

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
            Costs not assigned to any asset:{' '}
            <Money value={money(unallocatedCosts)} options={AMOUNT_FORMAT} /> {quoteCurrency}, from
            stablecoin conversions or from swaps into units with no known cost.
          </p>
        )}
      </div>
    </>
  );
}
