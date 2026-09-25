import { useState } from 'react';

import { truncateAddress } from '@/lib/addresses';

interface AddressProps {
  readonly value: string;
  /**
   * What owns this address - a wallet's label, or a chain name plus its truncated address -
   * folded into the copy button's accessible name as "Copy address of {name}". A page with
   * more than one address on screen otherwise gives every "Copy address" button the same
   * accessible name, which a screen reader user cannot tell apart. Omitted, the button's
   * name stays the plain "Copy address" its visible text already gives it.
   */
  readonly name?: string;
}

type CopyState = 'idle' | 'copied' | 'failed';

/**
 * Renders an address truncated for display, with its full form in `title` and a button
 * that copies it to the clipboard.
 *
 * A successful copy is announced through `role="status"`, per the spec's accessibility
 * rules for a wait or a confirmation that is not itself an error. A failed copy - the
 * clipboard API needs a secure context, which is not guaranteed everywhere - says so and
 * falls back to showing the full address in a selectable element, so the owner can still
 * copy it by hand instead of facing a button that silently did nothing.
 *
 * The full address never leaves this component for a route, a query string or a log: it is
 * rendered and, on request, handed to `navigator.clipboard` alone.
 */
export function Address({ value, name }: AddressProps) {
  const [copyState, setCopyState] = useState<CopyState>('idle');

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  }

  return (
    <span className="address">
      <span className="address-short" title={value}>
        {truncateAddress(value)}
      </span>
      <button
        type="button"
        aria-label={name !== undefined ? `Copy address of ${name}` : undefined}
        onClick={() => {
          void handleCopy();
        }}
      >
        Copy address
      </button>
      {copyState === 'copied' && <span role="status">Address copied.</span>}
      {copyState === 'failed' && (
        <span role="status">
          Could not copy automatically. Select the address to copy it by hand:{' '}
          <span className="address-full">{value}</span>
        </span>
      )}
    </span>
  );
}
