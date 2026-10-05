import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { WalletValueChart } from '@/pages/dashboard/WalletValueChart';
import { currentBalances, unreadBalance, walletBalance } from '@/test/fixtures';

/** The plot rows of `region`, in order. */
function barRows(region: HTMLElement): HTMLElement[] {
  return Array.from(region.querySelectorAll<HTMLElement>('.bar-row'));
}

/** One text per row: what `selector` holds in each. */
function texts(rows: readonly HTMLElement[], selector: string): (string | undefined)[] {
  return rows.map((row) => row.querySelector(selector)?.textContent);
}

describe('WalletValueChart', () => {
  it('draws each valued wallet largest first, named as the tables name it', () => {
    render(
      <WalletValueChart
        data={currentBalances({
          quote_currency: 'USD',
          wallets: [
            walletBalance({ wallet_id: 1, label: 'Spending', value: '9.0000000000' }),
            walletBalance({ wallet_id: 2, label: 'Cold storage', value: '10.0000000000' }),
            walletBalance({
              wallet_id: 3,
              chain_key: 'kaspa',
              label: null,
              asset_symbol: 'KAS',
              value: '0.5000000000',
            }),
          ],
        })}
      />,
    );

    const region = screen.getByRole('region', { name: 'Value by wallet' });
    expect(region).toHaveTextContent('USDT');
    const rows = barRows(region);
    // By value, not by the characters of the string: "9" would sort after "10".
    expect(texts(rows, '.bar-name')).toEqual(['Cold storage', 'Spending', 'Kaspa wallet #3']);
    expect(texts(rows, '.bar-value')).toEqual(['10.00', '9.00', '0.50']);
    expect(texts(rows, '.bar-sub')).toEqual(['BTC', 'BTC', 'KAS']);
  });

  it('paints a wallet in its asset colour, the same for two wallets of one asset', () => {
    render(
      <WalletValueChart
        data={currentBalances({
          wallets: [
            walletBalance({ wallet_id: 1, value: '2.0000000000' }),
            walletBalance({ wallet_id: 2, value: '1.0000000000' }),
            walletBalance({
              wallet_id: 3,
              chain_key: 'kaspa',
              asset_symbol: 'KAS',
              value: '0.5000000000',
            }),
          ],
        })}
      />,
    );

    const colors = barRows(screen.getByRole('region', { name: 'Value by wallet' })).map((row) =>
      row.style.getPropertyValue('--bar-color'),
    );
    expect(colors[0]).not.toBe('');
    expect(colors[1]).toBe(colors[0]);
    expect(colors[2]).not.toBe(colors[0]);
  });

  it('leaves out a wallet with no value: a bar of nothing would read as worth zero', () => {
    render(
      <WalletValueChart
        data={currentBalances({
          complete: false,
          wallets: [
            walletBalance({ wallet_id: 1, label: 'Cold storage' }),
            unreadBalance({ id: 2, chain_key: 'bitcoin', label: 'Not read yet' }),
          ],
        })}
      />,
    );

    const region = screen.getByRole('region', { name: 'Value by wallet' });
    expect(texts(barRows(region), '.bar-name')).toEqual(['Cold storage']);
    expect(region).not.toHaveTextContent('Not read yet');
  });

  it('draws nothing at all when no wallet has a value', () => {
    const { container } = render(
      <WalletValueChart
        data={currentBalances({
          complete: false,
          wallets: [unreadBalance({ id: 1, chain_key: 'bitcoin', label: 'Not read yet' })],
        })}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
