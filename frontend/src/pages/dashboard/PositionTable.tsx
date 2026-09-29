import type { Exclusion, Position } from '@/api/accounting';
import { Money } from '@/components/Money';
import { RelativeTime } from '@/components/RelativeTime';
import {
  AMOUNT_FORMAT,
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  MARKET_VALUE_UNAVAILABLE_MESSAGES,
  SIGNED_FORMAT,
  UNIT_PRICE_FORMAT,
} from '@/lib/accounting';
import { isZeroMoney, money, type FormatMoneyOptions } from '@/lib/money';
import { ReturnPercent } from '@/pages/dashboard/ReturnPercent';

interface OptionalAmountProps {
  readonly value: string | null;
  readonly options: FormatMoneyOptions;
}

/** An amount, or "—" for one the backend could not compute. Never a `0`. */
function OptionalAmount({ value, options }: OptionalAmountProps) {
  return value === null ? '—' : <Money value={money(value)} options={options} />;
}

/**
 * A text badge. The trailing space is not decoration: the badges sit in a flex container,
 * where CSS ignores it, but the row header's accessible name is built from the text nodes,
 * and without it "Unknown cost" and "Not in totals" would run together as "Unknown costNot
 * in totals".
 */
function Badge({ children }: { readonly children: string }) {
  return (
    <>
      <span className="badge">{children}</span>{' '}
    </>
  );
}

interface PositionRowProps {
  readonly position: Position;
  /** Whether `totals.excluded` names this asset: its row is not in the totals above. */
  readonly excluded: boolean;
}

/**
 * The market value cell: the amount, or the sentence for why there is none. A holding with
 * no value is never a `0.00` - it is a holding nobody could value.
 *
 * No fallback for a missing value with a missing reason: the backend pairs the two by
 * construction (`valuation.py`, "never one without the other"), so that shape is not one it
 * can write, and inventing a reason for it would be inventing a fact (spec 022, R1).
 */
function marketValueCell(position: Position) {
  const reason = position.market_value_unavailable_reason;

  if (position.market_value !== null) {
    return <Money value={money(position.market_value)} options={AMOUNT_FORMAT} />;
  }

  return (
    <span className="unavailable">
      {reason !== null && MARKET_VALUE_UNAVAILABLE_MESSAGES[reason]}
    </span>
  );
}

function PositionRow({ position, excluded }: PositionRowProps) {
  const hasUnknownBasis = !isZeroMoney(money(position.unknown_basis_quantity));

  return (
    <tr>
      <th scope="row">
        {position.asset}{' '}
        <span className="row-badges">
          {position.flags.map((flag) => (
            <Badge key={flag}>{FLAG_BADGES[flag]}</Badge>
          ))}
          {excluded && <Badge>Not in totals</Badge>}
        </span>
      </th>
      <td className="num">
        <Money value={money(position.quantity)} />
        {hasUnknownBasis && (
          <>
            {' '}
            (<Money value={money(position.unknown_basis_quantity)} /> with no known cost)
          </>
        )}
      </td>
      <td className="num">
        <OptionalAmount value={position.average_cost} options={UNIT_PRICE_FORMAT} />
      </td>
      <td className="num">
        <Money value={money(position.total_invested)} options={AMOUNT_FORMAT} />
      </td>
      <td className="num">
        {position.price === null ? (
          '—'
        ) : (
          <>
            <Money value={money(position.price.amount)} options={UNIT_PRICE_FORMAT} />
            {position.price.stale && (
              <>
                {' '}
                (stale, as of <RelativeTime value={position.price.as_of} />)
              </>
            )}
          </>
        )}
      </td>
      <td className="num">{marketValueCell(position)}</td>
      <td className="num">
        <OptionalAmount value={position.unrealized_pnl} options={SIGNED_FORMAT} />
      </td>
      <td className="num">
        <ReturnPercent value={position.unrealized_return_pct} />
      </td>
    </tr>
  );
}

interface PositionTableProps {
  /** The held positions only, in the endpoint's order. */
  readonly positions: readonly Position[];
  readonly excluded: readonly Exclusion[];
  readonly quoteCurrency: string;
}

/**
 * One row per held asset, with the data-quality flags on their rows and a legend under the
 * table for each flag that appears in it.
 *
 * The currency is in the column headers rather than on every cell, which is what keeps
 * eight columns inside the `.app` column at 1280 px. Below that the table sits in a
 * focusable scroll container with its first column sticky, so the page itself never scrolls
 * sideways and a row stays readable while its figures are scrolled into view.
 */
export function PositionTable({ positions, excluded, quoteCurrency }: PositionTableProps) {
  const excludedAssets = new Set(excluded.map((entry) => entry.asset));
  // Alphabetical, like the backend's own order for a position's flags, so the legend does
  // not reshuffle when the first row that carries a flag changes.
  const flags = Array.from(new Set(positions.flatMap((position) => position.flags))).sort();

  return (
    <>
      <h3 id="positions-heading">Per asset</h3>
      {positions.length === 0 ? (
        <p>Nothing is held right now.</p>
      ) : (
        <>
          {/* A focusable region, so a keyboard user can scroll a table wider than the
              screen. See the `no-noninteractive-tabindex` allowance in eslint.config.js. */}
          <div
            className="table-scroll"
            role="region"
            aria-labelledby="positions-heading"
            tabIndex={0}
          >
            <table className="position-table">
              <thead>
                <tr>
                  <th scope="col">Asset</th>
                  <th scope="col" className="num">
                    Quantity
                  </th>
                  <th scope="col" className="num">
                    Average cost ({quoteCurrency})
                  </th>
                  <th scope="col" className="num">
                    Invested ({quoteCurrency})
                  </th>
                  <th scope="col" className="num">
                    Price ({quoteCurrency})
                  </th>
                  <th scope="col" className="num">
                    Market value ({quoteCurrency})
                  </th>
                  <th scope="col" className="num">
                    Unrealized P&amp;L ({quoteCurrency})
                  </th>
                  <th scope="col" className="num">
                    Return
                  </th>
                </tr>
              </thead>
              <tbody>
                {positions.map((position) => (
                  <PositionRow
                    key={position.asset}
                    position={position}
                    excluded={excludedAssets.has(position.asset)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {flags.length > 0 && (
            <dl className="flag-legend">
              {flags.map((flag) => (
                <div key={flag}>
                  <dt>
                    <Badge>{FLAG_BADGES[flag]}</Badge>
                  </dt>
                  <dd>{FLAG_EXPLANATIONS[flag]}</dd>
                </div>
              ))}
            </dl>
          )}
        </>
      )}
    </>
  );
}
