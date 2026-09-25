import type { CurrentBalances, WalletBalance } from '@/api/balances';
import { Money } from '@/components/Money';
import { RelativeTime } from '@/components/RelativeTime';
import { addMoney, money, type Money as MoneyValue } from '@/lib/money';
import { PRICE_UNAVAILABLE_MESSAGES, type PriceUnavailable } from '@/lib/prices';

const FIAT_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
/**
 * A unit price is not a value: KAS at 0.084912345678 EUR rounds to "0.08" under
 * `FIAT_OPTIONS`, and a value column built from that rounded price no longer multiplies
 * out to what "Value" shows. Prices get more room - up to 8 fractional digits, the same
 * ceiling `formatMoney` defaults to - while values and the total stay at 2, because a
 * value is the thing a person reads at face value.
 */
const PRICE_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 8 };

interface AssetRow {
  readonly assetSymbol: string;
  /** `null` when every wallet holding this asset is unread. */
  readonly quantity: MoneyValue | null;
  readonly excludedUnreadCount: number;
  readonly price: WalletBalance['price'];
  /** `null` when unpriced, or when no wallet holding this asset has been read. */
  readonly value: MoneyValue | null;
  readonly unpricedReason: PriceUnavailable | null;
}

// Generic over `T extends WalletBalance` rather than fixed to `WalletBalance`, so that
// `.filter(hasQuantity)` on an already-narrowed array (e.g. after `.filter(hasValue)`)
// keeps the earlier narrowing instead of widening back to plain `WalletBalance`.
function hasQuantity<T extends WalletBalance>(wallet: T): wallet is T & { quantity: string } {
  return wallet.quantity !== null;
}

function hasValue<T extends WalletBalance>(wallet: T): wallet is T & { value: string } {
  return wallet.value !== null;
}

function sumMoney(values: readonly string[]): MoneyValue {
  return values.reduce<MoneyValue>((sum, value) => addMoney(sum, money(value)), money('0'));
}

/**
 * Groups the wallet rows the response already carries into one row per `asset_symbol`,
 * summing with `addMoney` over the exact strings the backend sent - see the spec's "Asset
 * rows are sums of the wallet rows the response already carries" for why this sums here
 * rather than trusting a second, backend-computed total for the same numbers.
 */
function buildAssetRows(data: CurrentBalances): AssetRow[] {
  const bySymbol = new Map<string, WalletBalance[]>();
  for (const wallet of data.wallets) {
    const group = bySymbol.get(wallet.asset_symbol);
    if (group === undefined) {
      bySymbol.set(wallet.asset_symbol, [wallet]);
    } else {
      group.push(wallet);
    }
  }

  const unpricedBySymbol = new Map(
    data.unpriced.map((holding) => [holding.asset_symbol, holding.reason]),
  );

  const rows = Array.from(bySymbol.entries()).map(([assetSymbol, walletRows]) => {
    const readRows = walletRows.filter(hasQuantity);
    const excludedUnreadCount = walletRows.length - readRows.length;

    if (readRows.length === 0) {
      return {
        assetSymbol,
        quantity: null,
        excludedUnreadCount,
        price: null,
        value: null,
        unpricedReason: null,
      };
    }

    const quantity = sumMoney(readRows.map((wallet) => wallet.quantity));
    const price = readRows.find((wallet) => wallet.price !== null)?.price ?? null;
    const valuedRows = readRows.filter(hasValue);
    const value = valuedRows.length > 0 ? sumMoney(valuedRows.map((wallet) => wallet.value)) : null;
    const unpricedReason = value === null ? (unpricedBySymbol.get(assetSymbol) ?? null) : null;

    return { assetSymbol, quantity, excludedUnreadCount, price, value, unpricedReason };
  });

  return rows.sort((a, b) => a.assetSymbol.localeCompare(b.assetSymbol));
}

interface AssetTableProps {
  readonly data: CurrentBalances;
}

/** One row per asset: quantity, price and fiat value, summed from the wallet rows. */
export function AssetTable({ data }: AssetTableProps) {
  const rows = buildAssetRows(data);

  return (
    <section aria-labelledby="assets-heading">
      <h2 id="assets-heading">Assets</h2>
      <table>
        <thead>
          <tr>
            <th scope="col">Asset</th>
            <th scope="col">Quantity</th>
            <th scope="col">Price</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.assetSymbol}>
              <th scope="row">{row.assetSymbol}</th>
              <td>
                {row.quantity === null ? (
                  'Not read yet'
                ) : (
                  <>
                    <Money value={row.quantity} /> {row.assetSymbol}
                    {row.excludedUnreadCount > 0 &&
                      ` (excludes ${String(row.excludedUnreadCount)} wallet${
                        row.excludedUnreadCount === 1 ? '' : 's'
                      } not yet read)`}
                  </>
                )}
              </td>
              <td>
                {row.price !== null ? (
                  <>
                    <Money value={money(row.price.amount)} options={PRICE_OPTIONS} />{' '}
                    {data.quote_currency}
                    {row.price.stale && (
                      <>
                        {' '}
                        (stale, as of <RelativeTime value={row.price.as_of} />)
                      </>
                    )}
                  </>
                ) : row.unpricedReason !== null ? (
                  PRICE_UNAVAILABLE_MESSAGES[row.unpricedReason]
                ) : (
                  '—'
                )}
              </td>
              <td>
                {row.value === null ? (
                  '—'
                ) : (
                  <>
                    <Money value={row.value} options={FIAT_OPTIONS} /> {data.quote_currency}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
