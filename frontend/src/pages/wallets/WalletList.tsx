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

/** What to focus once a settled archive or restore is confirmed by a fresh wallet list. */
interface PendingFocus {
  readonly walletId: number;
  /**
   * `wallets.data`'s reference at the moment the action settled - acted on only once
   * `wallets.data` is a *different* reference, never against the list as it stood before.
   *
   * Reference identity, not a timestamp: this used to compare `wallets.dataUpdatedAt`
   * against a captured wall-clock reading, which breaks two ways - a clock that steps
   * backwards between the list loading and the action settling leaves focus on `<body>`,
   * and a clock that does not advance at all, which is how every test in this suite runs
   * under a fixed `Date`, means focus never moves. TanStack Query's structural sharing
   * keeps `data` at the same reference across a refetch whose content is unchanged, and
   * gives it a new one whenever the content differs - which an archive or a restore always
   * does, since either flips `archived` - so comparing references needs no clock at all.
   */
  readonly dataBefore: readonly Wallet[] | undefined;
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
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const controlRefs = useRef(new Map<number, HTMLButtonElement>());
  // A ref, not `useState`: nothing here is ever read during render - it only tells the
  // effect below what to do once the *next* fetch lands - so there is no state for React to
  // synchronise into the DOM, and driving it through `useState` would mean calling
  // `setState` from inside the very effect that reacts to it, which `eslint-plugin-react-hooks`
  // rightly flags as a cascading-render pattern to avoid. The effect still re-runs on its
  // own, because setting this ref always precedes the query's own state update (the
  // invalidated refetch) that changes `wallets.data`/`wallets.dataUpdatedAt`.
  const pendingFocusRef = useRef<PendingFocus | null>(null);

  function registerControlRef(walletId: number, element: HTMLButtonElement | null): void {
    if (element === null) {
      controlRefs.current.delete(walletId);
    } else {
      controlRefs.current.set(walletId, element);
    }
  }

  function notifyActionSettled(walletId: number): void {
    pendingFocusRef.current = { walletId, dataBefore: wallets.data };
  }

  useEffect(() => {
    const pending = pendingFocusRef.current;
    const data = wallets.data;

    if (pending === null || data === undefined || data === pending.dataBefore) {
      // Nothing pending; or a query key switched to one never fetched before - toggling
      // "Show archived" right after an action lands on `data: undefined` for a beat, and
      // there is nothing to check presence against yet; or the refetch the action
      // triggered has not landed yet, which reference equality against `dataBefore` is
      // what actually detects, with no clock involved (see `PendingFocus.dataBefore`).
      // Any of the three means: not yet, wait for the next `wallets.data` to come in.
      return;
    }

    const stillShown = data.some((wallet) => wallet.id === pending.walletId);
    if (stillShown) {
      controlRefs.current.get(pending.walletId)?.focus();
    } else {
      headingRef.current?.focus();
    }
    pendingFocusRef.current = null;
  }, [wallets.data]);

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
