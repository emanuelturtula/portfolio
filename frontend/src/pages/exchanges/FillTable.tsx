import type { ExchangeFill } from '@/api/exchanges';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { Money } from '@/components/Money';
import { UNIT_PRICE_FORMAT } from '@/lib/accounting';
import { EXCHANGES } from '@/lib/exchanges';
import {
  DERIVED_LEGEND,
  DERIVED_MARKER,
  FILL_SIDE_LABELS,
  NO_FEE,
  NO_ORDER_ID,
  NOT_IN_USDT,
} from '@/lib/fills';
import { money } from '@/lib/money';

/**
 * "(derived)" after a value that rests on a quote value the venue did not send. It goes on the
 * quote value and, for a USDT-quoted fill, on the USDT value too: that one is the same number
 * (`usdt_value` is the stored `quote_quantity`), and the figure the totals add up is the one
 * whose provenance the owner most needs to see.
 */
function DerivedMarker() {
  return (
    <>
      {' '}
      <span className="derived">{DERIVED_MARKER}</span>
    </>
  );
}

interface FillRowProps {
  readonly fill: ExchangeFill;
}

/**
 * One fill, as stored. Every amount goes through `<Money>`, so the exact wire string sits in
 * its `<data value>`; nothing here parses or adds one.
 *
 * - **The side is a word.** "Buy" or "Sell", never a colour on its own.
 * - **Amounts carry their asset in the cell**, because the quote asset differs from row to
 *   row: a header cannot name a unit that changes.
 * - **A quote value the venue did not send** is marked "(derived)" - on the USDT value as
 *   well, when there is one - and explained under the table.
 * - **A fill quoted in something other than USDT has no USDT value.** The cell says so rather
 *   than showing a dash or a zero, since a zero would read as "worth nothing".
 * - **A zero fee has no fee asset**, and reads "None".
 * - **A missing order id** reads "none", never an empty cell.
 */
function FillRow({ fill }: FillRowProps) {
  return (
    <tr>
      <th scope="row">
        <AbsoluteTime value={fill.executed_at} />
      </th>
      <td>{EXCHANGES[fill.exchange_key].name}</td>
      <td>
        {fill.base_asset}/{fill.quote_asset}
      </td>
      <td>{FILL_SIDE_LABELS[fill.side]}</td>
      <td className="num">
        <Money value={money(fill.quantity)} /> {fill.base_asset}
      </td>
      <td className="num">
        <Money value={money(fill.price)} options={UNIT_PRICE_FORMAT} /> {fill.quote_asset}
      </td>
      <td className="num">
        <Money value={money(fill.quote_quantity)} options={UNIT_PRICE_FORMAT} /> {fill.quote_asset}
        {fill.quote_quantity_derived && <DerivedMarker />}
      </td>
      <td className="num">
        {fill.usdt_value === null ? (
          NOT_IN_USDT
        ) : (
          <>
            <Money value={money(fill.usdt_value)} options={UNIT_PRICE_FORMAT} />
            {fill.quote_quantity_derived && <DerivedMarker />}
          </>
        )}
      </td>
      <td className="num">
        {fill.fee_asset === null ? (
          NO_FEE
        ) : (
          <>
            <Money value={money(fill.fee_amount)} /> {fill.fee_asset}
          </>
        )}
      </td>
      <td className="order-id">
        {fill.order_id ?? <span className="unavailable">{NO_ORDER_ID}</span>}
      </td>
    </tr>
  );
}

interface FillTableProps {
  readonly fills: readonly ExchangeFill[];
}

/**
 * One page of fills, newest first. The table sits in a focusable, labelled scroll region:
 * ten columns do not fit a phone, and a scroll container a keyboard cannot focus cannot be
 * scrolled from one. The page itself never scrolls sideways. See `PositionTable` for the
 * pattern and the lint allowance (`region`) that permits the `tabIndex`.
 *
 * Its heading is rendered by the caller, so the caller decides the level and owns the id the
 * region is labelled by.
 */
export function FillTable({ fills }: FillTableProps) {
  return (
    <>
      <div className="table-scroll" role="region" aria-labelledby="fills-heading" tabIndex={0}>
        <table className="data-table fill-table">
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Exchange</th>
              <th scope="col">Pair</th>
              <th scope="col">Side</th>
              <th scope="col" className="num">
                Quantity
              </th>
              <th scope="col" className="num">
                Price
              </th>
              <th scope="col" className="num">
                Quote value
              </th>
              <th scope="col" className="num">
                USDT value
              </th>
              <th scope="col" className="num">
                Fee
              </th>
              <th scope="col">Order id</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((fill) => (
              <FillRow key={fill.id} fill={fill} />
            ))}
          </tbody>
        </table>
      </div>
      {fills.some((fill) => fill.quote_quantity_derived) && (
        <p className="hint">{DERIVED_LEGEND}</p>
      )}
    </>
  );
}
