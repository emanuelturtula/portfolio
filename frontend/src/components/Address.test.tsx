import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Address } from '@/components/Address';
import { ADDRESSES } from '@/test/fixtures';

function renderAddress(value: string) {
  return render(
    <StrictMode>
      <Address value={value} />
    </StrictMode>,
  );
}

describe('Address', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the truncated form with the full address in its title', () => {
    renderAddress(ADDRESSES.kasPrimary);

    const short = screen.getByText('kaspatest:qxaqrl…gdmpks');
    expect(short).toHaveAttribute('title', ADDRESSES.kasPrimary);
    // The full address is not on screen until the owner asks for it.
    expect(screen.queryByText(ADDRESSES.kasPrimary)).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('copies the full address', async () => {
    // `userEvent.setup()` installs a clipboard stub on `navigator`, so what was
    // written can be read back.
    const user = userEvent.setup();
    renderAddress(ADDRESSES.btcSegwit);

    await user.click(screen.getByRole('button', { name: 'Copy address' }));

    // The full address, not the truncated one on screen.
    expect(await navigator.clipboard.readText()).toBe(ADDRESSES.btcSegwit);
    expect(await screen.findByRole('status')).toHaveTextContent(/address copied/i);
  });

  it('a failed copy shows the full address to select', async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(
      new DOMException('Write permission denied.', 'NotAllowedError'),
    );
    renderAddress(ADDRESSES.kasSecondary);

    await user.click(screen.getByRole('button', { name: 'Copy address' }));

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(/could not copy/i);
    expect(status).not.toHaveTextContent(/address copied/i);
    // Whole and on screen, so it can be selected by hand.
    expect(screen.getByText(ADDRESSES.kasSecondary)).toBeInTheDocument();
    // The browser's own wording is not the owner's business.
    expect(status).not.toHaveTextContent(/permission denied/i);
  });

  it('falls back to the full address when there is no clipboard at all', async () => {
    // `navigator.clipboard` is undefined outside a secure context. Calling a
    // method on it throws synchronously, which must land in the same fallback
    // rather than escaping as an unhandled error behind a dead button.
    const user = userEvent.setup();
    const descriptor = Object.getOwnPropertyDescriptor(window.navigator, 'clipboard');
    Object.defineProperty(window.navigator, 'clipboard', { value: undefined, configurable: true });

    try {
      renderAddress(ADDRESSES.btcLegacy);

      await user.click(screen.getByRole('button', { name: 'Copy address' }));

      expect(await screen.findByRole('status')).toHaveTextContent(/could not copy/i);
      expect(screen.getByText(ADDRESSES.btcLegacy)).toBeInTheDocument();
    } finally {
      if (descriptor === undefined) {
        Reflect.deleteProperty(window.navigator, 'clipboard');
      } else {
        Object.defineProperty(window.navigator, 'clipboard', descriptor);
      }
    }
  });

  it('can copy again after a failure', async () => {
    const user = userEvent.setup();
    const writeText = vi
      .spyOn(navigator.clipboard, 'writeText')
      .mockRejectedValueOnce(new Error('denied'));
    renderAddress(ADDRESSES.btcScript);

    await user.click(screen.getByRole('button', { name: 'Copy address' }));
    expect(await screen.findByRole('status')).toHaveTextContent(/could not copy/i);

    await user.click(screen.getByRole('button', { name: 'Copy address' }));

    expect(await screen.findByText(/address copied/i)).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(writeText).toHaveBeenLastCalledWith(ADDRESSES.btcScript);
  });
});
