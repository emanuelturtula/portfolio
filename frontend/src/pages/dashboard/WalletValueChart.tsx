import type { CurrentBalances, WalletBalance } from '@/api/balances';
import { BarChart, type BarRow } from '@/components/BarChart';
import { assetColors } from '@/lib/assetColors';
import { chainDisplayName } from '@/lib/chains';
import { currencyLabel } from '@/lib/currency';
import { compareMoney, money } from '@/lib/money';

const FIAT_OPTIONS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

function hasValue<T extends WalletBalance>(wallet: T): wallet is T & { value: string } {
  return wallet.value !== null;
}

/** The name the wallets table gives a row: its label, or its chain and id. */
function walletName(wallet: WalletBalance): string {
  return (
    wallet.label ?? `${chainDisplayName(wallet.chain_key)} wallet #${String(wallet.wallet_id)}`
  );
}

/**
 * What each wallet is worth, largest first, one bar each in its asset's colour. A wallet with
 * no value - not read yet, or its asset unpriced - has no bar: the tables say why, and a bar of
 * nothing would read as a wallet worth zero. With nothing valued there is no chart at all.
 */
export function WalletValueChart({ data }: { readonly data: CurrentBalances }) {
  const valued = data.wallets
    .filter(hasValue)
    .sort((a, b) => compareMoney(money(b.value), money(a.value)));

  if (valued.length === 0) {
    return null;
  }

  const colors = assetColors(data.wallets.map((wallet) => wallet.asset_symbol));
  const rows: BarRow[] = valued.map((wallet) => ({
    key: String(wallet.wallet_id),
    label: walletName(wallet),
    color: colors.get(wallet.asset_symbol),
    bars: [{ shade: 'solid', value: money(wallet.value) }],
    aside: <span className="bar-sub">{wallet.asset_symbol}</span>,
  }));

  return (
    <section className="card" aria-labelledby="wallet-value-heading">
      <div className="card-head">
        <h3 id="wallet-value-heading">Value by wallet</h3>
        <span className="page-meta">{currencyLabel(data.quote_currency)}</span>
      </div>
      <BarChart title="Value of each wallet" series={[]} rows={rows} format={FIAT_OPTIONS} />
    </section>
  );
}
