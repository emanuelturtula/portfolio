import type { PortfolioSummary } from '@/api/portfolio';
import { Money } from '@/components/Money';
import { assetColors } from '@/lib/assetColors';
import { formatMoney, money } from '@/lib/money';
import { AllocationDonut } from '@/pages/dashboard/AllocationDonut';

const QUANTITY = { maximumFractionDigits: 8 } as const;
const PRICE = { minimumFractionDigits: 2, maximumFractionDigits: 6 } as const;
const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;

/** A dash where there is no figure. An unpriced holding has no price, value or share - not 0. */
const NONE = '—';

/**
 * What is held, one row per asset, beside the donut of how the value splits.
 *
 * The table scrolls inside a focusable region of its own, so a phone narrower than it scrolls
 * the table and never the page (#118). Rendered only when something is held.
 */
export function Holdings({ summary }: { readonly summary: PortfolioSummary }) {
  const colors = assetColors(summary.holdings.map((holding) => holding.asset));

  return (
    <section className="card holdings" aria-labelledby="holdings-heading">
      <h2 id="holdings-heading">Holdings</h2>
      <div className="holdings-body">
        <AllocationDonut holdings={summary.holdings} colors={colors} />
        <div className="table-scroll" role="region" aria-label="Holdings table" tabIndex={0}>
          <table className="data-table holdings-table">
            <thead>
              <tr>
                <th scope="col">Asset</th>
                <th scope="col" className="num">
                  Amount
                </th>
                <th scope="col" className="num">
                  Price
                </th>
                <th scope="col" className="num">
                  Value (USDT)
                </th>
                <th scope="col" className="num">
                  Share
                </th>
              </tr>
            </thead>
            <tbody>
              {summary.holdings.map((holding) => (
                <tr key={holding.asset}>
                  <th scope="row">
                    <span className="asset-cell">
                      <span
                        className="swatch"
                        style={{ background: colors.get(holding.asset) }}
                        aria-hidden="true"
                      />
                      {holding.asset}
                    </span>
                  </th>
                  <td className="num">
                    <Money value={money(holding.quantity)} options={QUANTITY} />
                  </td>
                  <td className="num">
                    {holding.price === null ? (
                      NONE
                    ) : (
                      <Money value={money(holding.price)} options={PRICE} />
                    )}
                  </td>
                  <td className="num">
                    {holding.value === null ? (
                      NONE
                    ) : (
                      <Money value={money(holding.value)} options={FIAT} />
                    )}
                  </td>
                  <td className="num">
                    {holding.share_pct === null
                      ? NONE
                      : `${formatMoney(money(holding.share_pct), FIAT)} %`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
