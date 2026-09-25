import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import { describeApiError } from '@/api/client';
import { useArchiveWallet, useRestoreWallet, useWallets, type Wallet } from '@/api/wallets';
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
  /** Whether the list is currently showing archived wallets - what decides whether this
   * row survives its own archive (stays, and swaps to Restore) or not (leaves the list). */
  readonly includeArchived: boolean;
  /** Focuses the wallet list's own heading. Owned by the list, not the row: it is the one
   * stable element still on screen once an archived row leaves the active-only view. */
  readonly focusListHeading: () => void;
}

/**
 * One row: its own archive/restore controls, and the focus hand-off for both.
 *
 * **Archive in the active-only view is the simple case**: the row always leaves once the
 * list refetches, so `onSuccess` focuses the list heading directly and does not wait for
 * anything - the row is still on screen for one more render, which is harmless, because
 * focus has already moved off it before it goes.
 *
 * **Restore, and archive with "Show archived" on, are the case where the row stays** and
 * swaps which control it shows. Nothing here waits on the wallets query's data at all - by
 * the time either mutation is fired, the answer to "does this row survive" is already known
 * from `includeArchived`, and the *right* control to focus is decided from this row's own
 * `wallet.archived`, watched with a one-row effect instead of any cross-row machinery. See
 * `focusOnceFlipped` for the one subtlety: `onSuccess` can run before or after the row has
 * re-rendered with the flipped value, and both orders have to land on the right control.
 */
function WalletRow({ wallet, includeArchived, focusListHeading }: WalletRowProps) {
  const [confirming, setConfirming] = useState(false);
  const archiveMutation = useArchiveWallet();
  const restoreMutation = useRestoreWallet();
  const archiveButtonRef = useRef<HTMLButtonElement | null>(null);
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreButtonRef = useRef<HTMLButtonElement | null>(null);
  const name = rowName(wallet);

  // Synced after every render (in an effect - a ref may not be written during render
  // itself), so `onSuccess` - which runs later, off whatever `wallet` its own click closed
  // over - can read this row's *latest* `archived` rather than a stale one.
  const archivedRef = useRef(wallet.archived);
  useEffect(() => {
    archivedRef.current = wallet.archived;
  });

  // Set when a focus hand-off is waiting for `wallet.archived` to flip to the value the
  // action in flight expects. Row-local and consumed only by *this* row's own flip, which
  // is what keeps it safe from the bug an earlier, cross-row version of this had: an
  // unrelated list change (toggling "Show archived") never flips a row's `archived` - it
  // mounts or unmounts rows - so it can never wrongly satisfy this.
  const focusAfterFlipRef = useRef(false);

  useEffect(() => {
    if (!focusAfterFlipRef.current) {
      return;
    }
    focusAfterFlipRef.current = false;
    if (wallet.archived) {
      restoreButtonRef.current?.focus();
    } else {
      archiveButtonRef.current?.focus();
    }
  }, [wallet.archived]);

  /**
   * Arranges to focus whichever control corresponds to `expectArchived`, once the row is
   * showing it.
   *
   * The order between "the refetch has already flipped `wallet.archived`" and "`onSuccess`
   * runs" is not fixed. If the flip already happened - `archivedRef.current` already reads
   * `expectArchived` - the effect above already ran for it and found nothing pending, and
   * `wallet.archived` will not change *again* on its own, so this focuses immediately
   * instead of arming a hand-off nothing will ever trigger. Otherwise the flip is still
   * ahead, and the effect is what will catch it.
   */
  function focusOnceFlipped(expectArchived: boolean): void {
    if (archivedRef.current === expectArchived) {
      if (expectArchived) {
        restoreButtonRef.current?.focus();
      } else {
        archiveButtonRef.current?.focus();
      }
    } else {
      focusAfterFlipRef.current = true;
    }
  }

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
            ref={restoreButtonRef}
            aria-label={`Restore ${name}`}
            onClick={() => {
              restoreMutation.mutate(wallet.id, {
                onSuccess: () => {
                  // Restore only ever appears with "Show archived" on, so the row always
                  // stays - there is no active-only-view case to branch on here.
                  focusOnceFlipped(false);
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
                    setConfirming(false);
                    if (includeArchived) {
                      focusOnceFlipped(true);
                    } else {
                      focusListHeading();
                    }
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
            ref={archiveButtonRef}
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
 * The wallet list, with its own loading, empty, error and success states - independent of
 * {@link WalletForm}, so a failure here never takes the add form down with it.
 *
 * Focus management for an archive or a restore is entirely each row's own concern (see
 * `WalletRow`); this component's only part in it is the one destination a row cannot own
 * itself - its own heading, which outlives every row and is where focus goes when an
 * archive removes a row from the active-only view.
 */
export function WalletList() {
  const [includeArchived, setIncludeArchived] = useState(false);
  const wallets = useWallets(includeArchived);
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  function focusListHeading(): void {
    headingRef.current?.focus();
  }

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
              includeArchived={includeArchived}
              focusListHeading={focusListHeading}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
