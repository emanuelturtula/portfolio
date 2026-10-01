import type { ReconciliationAsset } from '@/api/accounting';
import { Money } from '@/components/Money';
import { DIFFERENCE_LEGEND } from '@/lib/accounting';
import { SIGNED_QUANTITY_FORMAT } from '@/lib/fills';
import { money } from '@/lib/money';

interface ReconciliationTableProps {
  readonly assets: readonly ReconciliationAsset[];
  /** The id of the element that names this table: the heading, or the disclosure's summary. */
  readonly labelledBy: string;
}

/**
 * One row per asset: what the history accounts for, what the wallets and the exchanges hold,
 * and the difference between the two.
 *
 * **Every figure is the server's.** The page adds nothing up: `difference` is the backend's
 * exact subtraction, and each quantity sits in a `<data value>` holding the string as it was
 * sent. The sign is text - `+` when the balances are above the history, `-` when below, zero
 * unsigned - and is never carried by colour alone.
 *
 * Inside the same kind of focusable, labelled scroll region the other tables use, so a phone
 * scrolls the table and never the page.
 */
export function ReconciliationTable({ assets, labelledBy }: ReconciliationTableProps) {
  return (
    <>
      <div className="table-scroll" role="region" aria-labelledby={labelledBy} tabIndex={0}>
        <table className="data-table data-table--sticky" aria-labelledby={labelledBy}>
          <thead>
            <tr>
              <th scope="col">Asset</th>
              <th scope="col" className="num">
                In history
              </th>
              <th scope="col" className="num">
                Wallets
              </th>
              <th scope="col" className="num">
                Exchanges
              </th>
              <th scope="col" className="num">
                Difference
              </th>
            </tr>
          </thead>
          <tbody>
            {assets.map((entry) => (
              <tr key={entry.asset}>
                <th scope="row">{entry.asset}</th>
                <td className="num">
                  <Money value={money(entry.history_quantity)} />
                </td>
                <td className="num">
                  <Money value={money(entry.wallet_quantity)} />
                </td>
                <td className="num">
                  <Money value={money(entry.exchange_quantity)} />
                </td>
                <td className="num">
                  <Money value={money(entry.difference)} options={SIGNED_QUANTITY_FORMAT} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">{DIFFERENCE_LEGEND}</p>
    </>
  );
}
