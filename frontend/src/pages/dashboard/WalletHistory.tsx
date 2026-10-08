import { useId, useState } from 'react';

import type { WalletBalance } from '@/api/balances';
import { DEFAULT_HISTORY_RANGE, useWalletValueHistory, type HistoryRange } from '@/api/history';
import { assetColors } from '@/lib/assetColors';
import { WALLET_EMPTY_WORDS } from '@/lib/history';
import { HistoryTitle, HistoryView, RangeSelector } from '@/pages/dashboard/ValueHistory';
import { walletName } from '@/pages/dashboard/WalletValueChart';

interface WalletHistoryChartProps {
  readonly wallet: WalletBalance;
  readonly range: HistoryRange;
}

/** One wallet's line, in its asset's colour, with its quantity in the hover card. */
function WalletHistoryChart({ wallet, range }: WalletHistoryChartProps) {
  const history = useWalletValueHistory(wallet.wallet_id, range);
  const asset = wallet.asset_symbol;

  return (
    <HistoryView
      query={history}
      subject={`${walletName(wallet)} value`}
      color={assetColors([asset]).get(asset)}
      asset={asset}
      emptyText={WALLET_EMPTY_WORDS}
    />
  );
}

/**
 * The Wallets page's chart (spec 037): one wallet's value on each day of the chosen range, the
 * wallet picked from the ones the tables above list - the active wallets - and named as they
 * name it. The first is shown to begin with; if the chosen one leaves the list (archived from
 * another tab), the chart goes back to the first rather than to a wallet nobody chose.
 *
 * The list is the current balances' rows, which the Wallets page has already read, so this
 * section adds no second way for the wallet list to fail. Nothing to choose from draws nothing.
 */
export function WalletHistory({ wallets }: { readonly wallets: readonly WalletBalance[] }) {
  const selectId = useId();
  const [chosen, setChosen] = useState<string>();
  const [range, setRange] = useState<HistoryRange>(DEFAULT_HISTORY_RANGE);
  const wallet = wallets.find((entry) => String(entry.wallet_id) === chosen) ?? wallets[0];

  if (wallet === undefined) {
    return null;
  }

  return (
    <section className="card history-card" aria-labelledby="wallet-history-heading">
      <div className="card-head">
        <HistoryTitle id="wallet-history-heading">Wallet value over time</HistoryTitle>
        <RangeSelector value={range} onChange={setRange} />
      </div>
      <div className="field history-wallet">
        <label htmlFor={selectId}>Wallet</label>
        <select
          id={selectId}
          value={String(wallet.wallet_id)}
          onChange={(event) => {
            setChosen(event.target.value);
          }}
        >
          {wallets.map((entry) => (
            <option key={entry.wallet_id} value={String(entry.wallet_id)}>
              {walletName(entry)} ({entry.asset_symbol})
            </option>
          ))}
        </select>
      </div>
      <WalletHistoryChart wallet={wallet} range={range} />
    </section>
  );
}
