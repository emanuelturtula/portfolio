import type { ExchangeFillTotals } from '@/api/exchanges';
import { Money } from '@/components/Money';
import { AMOUNT_FORMAT, SIGNED_FORMAT, UNIT_PRICE_FORMAT } from '@/lib/accounting';
import {
  describeUnvaluedFills,
  FEES_LEGEND,
  NET_LEGEND,
  SIGNED_QUANTITY_FORMAT,
  SIGNED_QUOTE_FORMAT,
} from '@/lib/fills';
import { formatCount } from '@/lib/exchanges';
import { money } from '@/lib/money';

interface FillTotalsViewProps {
  readonly totals: ExchangeFillTotals;
}

/**
 * What the whole filtered set adds up to, whatever page is on screen: per base asset, in
 * USDT across assets, the fills that have no USDT value, and the fees.
 *
 * **Every figure is the server's.** The client holds one page of fills, so a sum here would
 * be a sum over the wrong set, and money is never added on this side anyway. Each amount is
 * a `<Money>`, whose `<data value>` carries the exact string.
 *
 * - **Signs are text.** A net is buys minus sells and can be negative over a filtered range;
 *   `+` and `-` say so without colour.
 * - **Quantities are never summed across assets**, so there is no total row. Only USDT is a
 *   common unit, and it is shown on its own.
 * - **A fill quoted in another asset is never converted.** It is counted in its asset's
 *   fills, left out of the USDT figures (and the row says how many), and summed per quote
 *   asset under "Not valued in USDT".
 */
export function FillTotalsView({ totals }: FillTotalsViewProps) {
  const { by_asset: byAsset, usdt, not_valued_in_usdt: notValued, fees } = totals;

  return (
    <section aria-labelledby="fill-totals-heading">
      <h4 id="fill-totals-heading">Totals</h4>

      <dl className="totals-summary">
        <div>
          <dt>USDT spent</dt>
          <dd>
            <Money value={money(usdt.spent)} options={AMOUNT_FORMAT} /> USDT
          </dd>
        </div>
        <div>
          <dt>USDT received</dt>
          <dd>
            <Money value={money(usdt.received)} options={AMOUNT_FORMAT} /> USDT
          </dd>
        </div>
        <div>
          <dt>USDT net</dt>
          <dd>
            <Money value={money(usdt.net)} options={SIGNED_FORMAT} /> USDT
          </dd>
        </div>
      </dl>

      <h5 id="fill-totals-assets-heading">Per asset</h5>
      <div
        className="table-scroll"
        role="region"
        aria-labelledby="fill-totals-assets-heading"
        tabIndex={0}
      >
        <table className="data-table data-table--sticky">
          <thead>
            <tr>
              <th scope="col">Asset</th>
              <th scope="col" className="num">
                Fills
              </th>
              <th scope="col" className="num">
                Bought
              </th>
              <th scope="col" className="num">
                Sold
              </th>
              <th scope="col" className="num">
                Net
              </th>
              <th scope="col" className="num">
                USDT spent
              </th>
              <th scope="col" className="num">
                USDT received
              </th>
              <th scope="col" className="num">
                USDT net
              </th>
            </tr>
          </thead>
          <tbody>
            {byAsset.map((asset) => (
              <tr key={asset.asset}>
                <th scope="row">
                  {asset.asset}
                  {asset.usdt_unvalued_fill_count > 0 && (
                    <>
                      {' '}
                      <span className="row-note">
                        {describeUnvaluedFills(asset.usdt_unvalued_fill_count)}
                      </span>
                    </>
                  )}
                </th>
                <td className="num">{formatCount(asset.fill_count)}</td>
                <td className="num">
                  <Money value={money(asset.bought)} />
                </td>
                <td className="num">
                  <Money value={money(asset.sold)} />
                </td>
                <td className="num">
                  <Money value={money(asset.net)} options={SIGNED_QUANTITY_FORMAT} />
                </td>
                <td className="num">
                  <Money value={money(asset.usdt_spent)} options={AMOUNT_FORMAT} />
                </td>
                <td className="num">
                  <Money value={money(asset.usdt_received)} options={AMOUNT_FORMAT} />
                </td>
                <td className="num">
                  <Money value={money(asset.usdt_net)} options={SIGNED_FORMAT} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">{NET_LEGEND}</p>

      {notValued.by_quote_asset.length > 0 && (
        <div>
          <h5 id="fill-totals-not-valued-heading">Not valued in USDT</h5>
          <p>
            {formatCount(notValued.fill_count)}{' '}
            {notValued.fill_count === 1 ? 'fill is' : 'fills are'} quoted in something other than
            USDT. They have no USDT value and are never converted, so they are summed here in their
            own quote asset, and left out of the USDT figures above.
          </p>
          <div
            className="table-scroll"
            role="region"
            aria-labelledby="fill-totals-not-valued-heading"
            tabIndex={0}
          >
            <table className="data-table data-table--sticky">
              <thead>
                <tr>
                  <th scope="col">Quote asset</th>
                  <th scope="col" className="num">
                    Fills
                  </th>
                  <th scope="col" className="num">
                    Spent
                  </th>
                  <th scope="col" className="num">
                    Received
                  </th>
                  <th scope="col" className="num">
                    Net
                  </th>
                </tr>
              </thead>
              <tbody>
                {notValued.by_quote_asset.map((quote) => (
                  <tr key={quote.quote_asset}>
                    <th scope="row">{quote.quote_asset}</th>
                    <td className="num">{formatCount(quote.fill_count)}</td>
                    <td className="num">
                      <Money value={money(quote.spent)} options={UNIT_PRICE_FORMAT} />
                    </td>
                    <td className="num">
                      <Money value={money(quote.received)} options={UNIT_PRICE_FORMAT} />
                    </td>
                    <td className="num">
                      <Money value={money(quote.net)} options={SIGNED_QUOTE_FORMAT} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {fees.length > 0 && (
        <div>
          <h5 id="fill-totals-fees-heading">Fees</h5>
          <p className="hint">{FEES_LEGEND}</p>
          <ul className="fee-list">
            {fees.map((fee) => (
              <li key={fee.asset}>
                <Money value={money(fee.amount)} /> {fee.asset}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
