import { useState } from 'react';

import type { Wallet } from '@/api/wallets';
import { truncateAddress } from '@/lib/addresses';

interface AddressProps {
  /**
   * The address, or - for a wallet whose `kind` is `extended_key` - the masked form the API
   * serves: four characters, an ellipsis, four characters. The full key never reaches the
   * browser, so there is nothing here to truncate further or to copy.
   */
  readonly value: string;
  /** What `value` is. Omitted, it is an address. */
  readonly kind?: Wallet['kind'];
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
 * An extended key's masked form, exactly as the API served it, beside a label saying what
 * it is. Deliberately has no copy button: copying the mask would put something on the
 * clipboard that looks like a key and is not one, and the real key is never sent here.
 */
function MaskedExtendedKey({ value }: { readonly value: string }) {
  return (
    <span className="address">
      <span className="address-short">{value}</span>
      <span className="badge">Extended key</span>
    </span>
  );
}

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
 *
 * With `kind="extended_key"` it renders the masked form instead, with a label and no copy
 * button - see {@link MaskedExtendedKey}.
 */
export function Address({ value, name, kind = 'address' }: AddressProps) {
  // A component of its own rather than an early return below: the hook in `CopyableAddress`
  // would otherwise be called conditionally.
  return kind === 'extended_key' ? (
    <MaskedExtendedKey value={value} />
  ) : (
    <CopyableAddress value={value} name={name} />
  );
}

function CopyableAddress({
  value,
  name,
}: {
  readonly value: string;
  readonly name: string | undefined;
}) {
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
