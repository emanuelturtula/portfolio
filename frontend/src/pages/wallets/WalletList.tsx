import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { useQueryClient } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import {
  useArchiveWallet,
  useRestoreWallet,
  useWallets,
  walletsQueryKey,
  type Wallet,
} from '@/api/wallets';
import { Address } from '@/components/Address';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { truncateAddress } from '@/lib/addresses';
import { chainDisplayName } from '@/lib/chains';

const ARCHIVE_CONSEQUENCE =
  'This wallet will stop being read and will leave the total. Archiving can be undone with Restore.';

/**
 * What identifies this row to a screen reader: the label, or else the chain name plus the
 * truncated address - the same fallback `WalletBalanceTable` uses on the dashboard. Folded
 * into every per-row control's accessible name, because every row's "Archive", "Restore"
 * and so on otherwise share one name across the whole list.
 */
function rowName(wallet: Wallet): string {
  return wallet.label ?? `${chainDisplayName(wallet.chain_key)} ${truncateAddress(wallet.address)}`;
}

interface WalletRowProps {
  readonly wallet: Wallet;
  /** Registers (or, given `null`, unregisters) this row's current primary control - the
   * Archive or Restore button, whichever is shown - so the list can return focus to it. */
  readonly registerControlRef: (walletId: number, element: HTMLButtonElement | null) => void;
  /** Tells the list an archive or a restore for this wallet just succeeded, so it can move
   * focus once the refetched list confirms whether the row is still shown. */
  readonly notifyActionSettled: (walletId: number) => void;
}

function WalletRow({ wallet, registerControlRef, notifyActionSettled }: WalletRowProps) {
  const [confirming, setConfirming] = useState(false);
  const archiveMutation = useArchiveWallet();
  const restoreMutation = useRestoreWallet();
  const archiveButtonRef = useRef<HTMLButtonElement | null>(null);
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null);
  const name = rowName(wallet);

  // `flushSync` forces the state update and its re-render to apply synchronously, so the
  // ref below already points at the newly-shown button by the time `.focus()` runs -
  // otherwise the button this call wants to focus has not been mounted yet. This is what
  // keeps the confirm step from ever leaving focus on `<body>`: no pressed control may
  // vanish without handing focus to whatever replaces it.
  function openConfirm() {
    flushSync(() => {
      setConfirming(true);
    });
    confirmButtonRef.current?.focus();
  }

  function cancelConfirm() {
    flushSync(() => {
      setConfirming(false);
    });
    archiveButtonRef.current?.focus();
  }

  return (
    <li className="wallet-row">
      <div className="wallet-row-main">
        <span className="wallet-chain">{chainDisplayName(wallet.chain_key)}</span>
        {wallet.label !== null && <span className="wallet-label">{wallet.label}</span>}
        <Address value={wallet.address} name={name} />
        {wallet.archived && <span className="badge">Archived</span>}
      </div>

      <div className="wallet-row-actions">
        {wallet.archived ? (
          <button
            type="button"
            ref={(element) => {
              registerControlRef(wallet.id, element);
            }}
            aria-label={`Restore ${name}`}
            onClick={() => {
              restoreMutation.mutate(wallet.id, {
                onSuccess: () => {
                  notifyActionSettled(wallet.id);
                },
              });
            }}
            disabled={restoreMutation.isPending}
          >
            Restore
          </button>
        ) : confirming ? (
          <span className="confirm-archive">
            <span>{ARCHIVE_CONSEQUENCE}</span>
            <button
              type="button"
              ref={confirmButtonRef}
              aria-label={`Confirm archive of ${name}`}
              onClick={() => {
                archiveMutation.mutate(wallet.id, {
                  onSuccess: () => {
                    // The row's own focus handling ends here: whether the row disappears
                    // (active-only view) or stays and swaps to Restore (archived shown) is
                    // not known yet - `wallets` has not refetched - so the list, which does
                    // know once it has, takes over from `notifyActionSettled`.
                    setConfirming(false);
                    notifyActionSettled(wallet.id);
                  },
                });
              }}
              disabled={archiveMutation.isPending}
            >
              Confirm archive
            </button>
            <button
              type="button"
              aria-label={`Cancel archiving ${name}`}
              onClick={cancelConfirm}
              disabled={archiveMutation.isPending}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            ref={(element) => {
              archiveButtonRef.current = element;
              registerControlRef(wallet.id, element);
            }}
            aria-label={`Archive ${name}`}
            onClick={openConfirm}
          >
            Archive
          </button>
        )}
      </div>

      {archiveMutation.isError && (
        <p role="alert">
          {describeApiError(archiveMutation.error, 'Could not archive the wallet. Try again.')}
        </p>
      )}
      {restoreMutation.isError && (
        <p role="alert">
          {describeApiError(restoreMutation.error, 'Could not restore the wallet. Try again.')}
        </p>
      )}
    </li>
  );
}

/**
 * A focus decision already made, waiting for the render it forced to commit.
 *
 * The decision - whether the wallet is still in the list - is resolved once, synchronously,
 * inside the mutation's own `onSuccess` (see `notifyActionSettled`), against the query
 * cache the invalidated refetch just populated. Nothing here is *re-evaluated* later against
 * a subsequent, unrelated list change: an earlier design instead left a pending request
 * sitting in a ref until some future `wallets.data` change happened to satisfy it, which is
 * exactly the bug this shape exists to rule out - a `DELETE` the server no-ops (already
 * archived, a retried request) never changes the list, so that pending request outlived its
 * own action and was later consumed by an unrelated "Show archived" toggle, stealing focus
 * from the checkbox the owner had just pressed. Resolving immediately and never revisiting
 * the decision removes the "unrelated later change" for a stale request to be mistaken for.
 */
interface FocusDecision {
  readonly walletId: number;
  readonly stillShown: boolean;
}

/**
 * The wallet list, with its own loading, empty, error and success states - independent of
 * {@link WalletForm}, so a failure here never takes the add form down with it.
 *
 * Owns the cross-row half of focus management: a row's own confirm/cancel transitions are
 * self-contained (see `WalletRow`), but where focus lands after an archive or a restore
 * *succeeds* depends on whether the row is still in the list once it has been refetched -
 * a fact only this component can see.
 */
export function WalletList() {
  const [includeArchived, setIncludeArchived] = useState(false);
  const wallets = useWallets(includeArchived);
  const queryClient = useQueryClient();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const controlRefs = useRef(new Map<number, HTMLButtonElement>());
  // The decision itself lives in a ref, not `useState`: it is written and consumed by the
  // effect below within the same commit cycle, and calling `setState` from inside that
  // effect to clear it is exactly what `eslint-plugin-react-hooks` flags as a
  // cascading-render pattern to avoid. `focusTick` is the real state - its only job is to
  // force the re-render the effect needs to run against, after `notifyActionSettled` has
  // written a fresh decision into the ref.
  const focusDecisionRef = useRef<FocusDecision | null>(null);
  const [focusTick, setFocusTick] = useState(0);

  function registerControlRef(walletId: number, element: HTMLButtonElement | null): void {
    if (element === null) {
      controlRefs.current.delete(walletId);
    } else {
      controlRefs.current.set(walletId, element);
    }
  }

  /**
   * Called from a mutation's own `onSuccess`, after the hook-level `onSuccess` - which
   * invalidates and awaits the refetch - has already run. The wallets query's cache entry
   * for `includeArchived`'s current value should be populated by now: the row that
   * triggered this call only exists because that entry was already populated when it
   * rendered, and an invalidated refetch replaces a cache entry, never clears it. `current`
   * is still checked rather than asserted, though - unlike a value this module derives
   * itself, a cache read is a boundary this function does not control, and `undefined`
   * here degrades to "focus the heading" rather than a runtime crash on `.some`.
   */
  function notifyActionSettled(walletId: number): void {
    const current = queryClient.getQueryData<Wallet[]>(walletsQueryKey(includeArchived));
    const stillShown = current?.some((wallet) => wallet.id === walletId) ?? false;
    focusDecisionRef.current = { walletId, stillShown };
    setFocusTick((tick) => tick + 1);
  }

  useEffect(() => {
    const decision = focusDecisionRef.current;
    if (decision === null) {
      return;
    }
    focusDecisionRef.current = null;

    if (decision.stillShown) {
      controlRefs.current.get(decision.walletId)?.focus();
    } else {
      headingRef.current?.focus();
    }
    // `focusTick` itself is never read here - its only job is to be a *different* number
    // each time, which is what makes this effect run again after `notifyActionSettled`.
  }, [focusTick]);

  return (
    <section aria-labelledby="wallet-list-heading">
      <div className="wallet-list-header">
        <h3 id="wallet-list-heading" ref={headingRef} tabIndex={-1}>
          Your wallets
        </h3>
        <label>
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => {
              setIncludeArchived(event.target.checked);
            }}
          />
          Show archived
        </label>
      </div>

      {wallets.isPending && <Skeleton label="Loading wallets…" />}

      {wallets.isError && (
        <ErrorState
          title="Could not load your wallets"
          description={describeApiError(
            wallets.error,
            'The backend could not be reached. Check that the API is running, then reload the page.',
          )}
          onRetry={() => {
            void wallets.refetch();
          }}
          headingLevel={4}
        />
      )}

      {wallets.isSuccess && wallets.data.length === 0 && (
        <EmptyState
          title="No wallets yet"
          description="Add a wallet above to start tracking its balance and value."
          headingLevel={4}
        />
      )}

      {wallets.isSuccess && wallets.data.length > 0 && (
        <ul className="wallet-list">
          {wallets.data.map((wallet) => (
            <WalletRow
              key={wallet.id}
              wallet={wallet}
              registerControlRef={registerControlRef}
              notifyActionSettled={notifyActionSettled}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
