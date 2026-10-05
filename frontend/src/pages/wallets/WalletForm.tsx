import { type RefObject, type SubmitEvent, useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import { ApiError, describeApiError } from '@/api/client';
import { useCreateWallet, type ChainKey } from '@/api/wallets';
import {
  addressHint,
  CHAIN_KEYS,
  chainDisplayName,
  isPrivateExtendedKey,
  PRIVATE_KEY_WARNING,
} from '@/lib/chains';

interface FormErrors {
  readonly address?: string;
  readonly label?: string;
  readonly chainKey?: string;
  readonly form?: readonly string[];
}

const DUPLICATE_ADDRESS_FALLBACK = 'This address is already registered for this chain.';
const EMPTY_ADDRESS_MESSAGE = 'An address is required.';
const GENERIC_FAILURE_MESSAGE = 'Could not add the wallet. Check your connection and try again.';
const PRIVATE_KEY_REFUSAL_NOTE = 'It was not sent, and the field has been cleared.';

/**
 * Refuses a private extended key the instant it is typed or pasted into a field - the one
 * value that must never leave this page. It is never stored, so it does not linger in the
 * form's state or on screen.
 *
 * Focus leaves the input for the alert, too. A key typed by hand is refused at its prefix,
 * and without this the rest of it would be typed into the emptied field and could be
 * submitted as a fragment. The alert is not editable, so those keystrokes go nowhere, and the
 * owner has to click back into the field on purpose. `flushSync` so the alert is mounted when
 * `focus()` runs; it is also what makes a screen reader read it.
 *
 * Used by both text fields: a key pasted into the label must not be sent any more than one
 * pasted into the address.
 */
function refusePrivateKey(
  clearField: (value: string) => void,
  showError: (message: string) => void,
  message: string,
  errorRef: RefObject<HTMLParagraphElement | null>,
): void {
  flushSync(() => {
    clearField('');
    showError(`${message} ${PRIVATE_KEY_REFUSAL_NOTE}`);
  });
  errorRef.current?.focus();
}

/**
 * Maps a failed `createWallet` call onto field-level errors, per the spec's "Field-level
 * errors" section.
 *
 * A 409 always renders under the address field: a duplicate is a fact about the address,
 * and the backend's detail never names which field to blame the way a 422 does. Every
 * other `ApiError` reads `problem.errors`, mapping each entry's `loc` - `["body", field]` -
 * onto the matching input; an entry at any other location joins `form`, rendered at the
 * bottom of the form instead of under a field that has nothing to do with it.
 *
 * **Every address refusal reaches the field through `msg` alone**, never through the
 * entry's `type`. The backend writes one fixed sentence per reason and none of them quotes
 * the value (`REJECTION_MESSAGES` in `domain/addresses.py`), so the reasons added for
 * extended keys - `private_key`, `extended_key_multisig`, `invalid_public_key` - and the
 * `extended_key` a Kaspa wallet can now get all render without a table of their own here:
 * a second copy of those sentences is how the two sides come to disagree about them.
 */
function mapMutationError(error: unknown): FormErrors {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return { address: describeApiError(error, DUPLICATE_ADDRESS_FALLBACK) };
    }

    const errors = error.problem.errors;
    if (errors !== undefined && errors.length > 0) {
      const result: { address?: string; label?: string; chainKey?: string } = {};
      const form: string[] = [];

      for (const entry of errors) {
        const field = entry.loc.at(-1);
        if (field === 'address') {
          result.address = entry.msg;
        } else if (field === 'label') {
          result.label = entry.msg;
        } else if (field === 'chain_key') {
          result.chainKey = entry.msg;
        } else {
          form.push(entry.msg);
        }
      }

      return form.length > 0 ? { ...result, form } : result;
    }
  }

  return { form: [describeApiError(error, GENERIC_FAILURE_MESSAGE)] };
}

function describedBy(...ids: (string | undefined)[]): string | undefined {
  const present = ids.filter((id): id is string => id !== undefined);
  return present.length > 0 ? present.join(' ') : undefined;
}

/**
 * The add-wallet form. Deliberately independent of the wallet list query: the list can
 * fail to load while this still works, because registering an address needs nothing this
 * form does not already have.
 */
export function WalletForm() {
  const [chainKey, setChainKey] = useState<ChainKey>('bitcoin');
  const [address, setAddress] = useState('');
  const [label, setLabel] = useState('');
  const [localAddressError, setLocalAddressError] = useState<string | undefined>(undefined);
  const [localLabelError, setLocalLabelError] = useState<string | undefined>(undefined);
  const addressInputRef = useRef<HTMLInputElement | null>(null);
  const addressErrorRef = useRef<HTMLParagraphElement | null>(null);
  const labelErrorRef = useRef<HTMLParagraphElement | null>(null);

  const mutation = useCreateWallet();

  const hint = addressHint(chainKey, address);
  const switchTarget = hint?.switchTo;

  const mutationErrors = mutation.isError ? mapMutationError(mutation.error) : undefined;
  const addressError = localAddressError ?? mutationErrors?.address;
  const labelError = localLabelError ?? mutationErrors?.label;
  const chainKeyError = mutationErrors?.chainKey;
  const formErrors = mutationErrors?.form;

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();

    if (address.trim() === '') {
      // The only client-side refusal at submit time: everything else is the server's
      // checksum to make, never a TypeScript re-implementation of it. No request is sent for
      // this one. (A private key is refused earlier still, as it is typed - see `onChange`.)
      setLocalAddressError(EMPTY_ADDRESS_MESSAGE);
      return;
    }

    setLocalAddressError(undefined);
    // `address` cannot hold a private extended key here: `onChange` is the only way a typed
    // value reaches it, and it refuses one before storing it. A second check on this line
    // would be a branch that can never run.
    mutation.mutate(
      { chain_key: chainKey, address, label: label.trim() === '' ? null : label },
      {
        onSuccess: () => {
          setAddress('');
          setLabel('');
        },
      },
    );
  }

  return (
    <form className="card" onSubmit={handleSubmit} noValidate aria-labelledby="add-wallet-heading">
      <h3 id="add-wallet-heading">Add a wallet</h3>

      <div className="field">
        <label htmlFor="wallet-chain">Chain</label>
        <select
          id="wallet-chain"
          value={chainKey}
          aria-invalid={chainKeyError !== undefined ? true : undefined}
          aria-describedby={describedBy(
            chainKeyError !== undefined ? 'wallet-chain-error' : undefined,
          )}
          onChange={(event) => {
            setChainKey(event.target.value as ChainKey);
            // Clears a field error left over from the previous submission - otherwise a
            // 409 or 422 from that attempt stays on screen, attached to a value the owner
            // has already changed, until they submit again.
            mutation.reset();
          }}
        >
          {CHAIN_KEYS.map((key) => (
            <option key={key} value={key}>
              {chainDisplayName(key)}
            </option>
          ))}
        </select>
        {chainKeyError !== undefined && (
          <p id="wallet-chain-error" role="alert">
            {chainKeyError}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="wallet-address">Address</label>
        <input
          id="wallet-address"
          ref={addressInputRef}
          type="text"
          value={address}
          // An extended public key reveals every address of a wallet, so it is not something
          // for the browser to remember, suggest back, or send to a spell-checking service.
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          aria-invalid={addressError !== undefined ? true : undefined}
          aria-describedby={describedBy(
            hint !== undefined ? 'wallet-address-hint' : undefined,
            addressError !== undefined ? 'wallet-address-error' : undefined,
          )}
          onChange={(event) => {
            const typed = event.target.value;
            mutation.reset();

            const typedHint = addressHint(chainKey, typed);
            if (typedHint?.blocksSubmission === true) {
              refusePrivateKey(
                setAddress,
                setLocalAddressError,
                typedHint.message,
                addressErrorRef,
              );
              return;
            }

            setAddress(typed);
            setLocalAddressError(undefined);
          }}
        />
        {hint !== undefined && (
          <p id="wallet-address-hint" className="hint">
            {hint.message}
            {switchTarget !== undefined && (
              <button
                type="button"
                onClick={() => {
                  setChainKey(switchTarget);
                  // Same reason the chain select and the address field reset it on their
                  // own change: an error from the chain just left behind must not linger,
                  // attached to a field the owner no longer means for it to describe.
                  mutation.reset();
                  // The address the owner already typed is what triggered this control, so
                  // focus returns to it rather than to the chain select they never touched.
                  addressInputRef.current?.focus();
                }}
              >
                Use {chainDisplayName(switchTarget)} instead
              </button>
            )}
          </p>
        )}
        {addressError !== undefined && (
          <p id="wallet-address-error" role="alert" tabIndex={-1} ref={addressErrorRef}>
            {addressError}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="wallet-label">Label</label>
        <input
          id="wallet-label"
          type="text"
          value={label}
          aria-invalid={labelError !== undefined ? true : undefined}
          aria-describedby={describedBy(
            labelError !== undefined ? 'wallet-label-error' : undefined,
          )}
          onChange={(event) => {
            const typed = event.target.value;
            mutation.reset();

            if (isPrivateExtendedKey(typed)) {
              refusePrivateKey(setLabel, setLocalLabelError, PRIVATE_KEY_WARNING, labelErrorRef);
              return;
            }

            setLabel(typed);
            setLocalLabelError(undefined);
          }}
        />
        {labelError !== undefined && (
          <p id="wallet-label-error" role="alert" tabIndex={-1} ref={labelErrorRef}>
            {labelError}
          </p>
        )}
      </div>

      {formErrors?.map((message, index) => (
        // Keyed by position, not by `message`: two distinct 422 entries can carry the
        // same text (e.g. two missing-field errors with identical wording), and a
        // duplicate key would be a React warning at best and a misrendered list at worst.
        <p key={index} role="alert">
          {message}
        </p>
      ))}

      <button type="submit" className="button-primary" disabled={mutation.isPending}>
        Add wallet
      </button>
    </form>
  );
}
