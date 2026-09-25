import { useState } from 'react';

import { describeApiError } from '@/api/client';
import { useArchiveWallet, useRestoreWallet, useWallets, type Wallet } from '@/api/wallets';
import { Address } from '@/components/Address';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { chainDisplayName } from '@/lib/chains';

const ARCHIVE_CONSEQUENCE =
  'This wallet will stop being read and will leave the total. Archiving can be undone with Restore.';

interface WalletRowProps {
  readonly wallet: Wallet;
}

function WalletRow({ wallet }: WalletRowProps) {
  const [confirming, setConfirming] = useState(false);
  const archiveMutation = useArchiveWallet();
  const restoreMutation = useRestoreWallet();

  return (
    <li className="wallet-row">
      <div className="wallet-row-main">
        <span className="wallet-chain">{chainDisplayName(wallet.chain_key)}</span>
        {wallet.label !== null && <span className="wallet-label">{wallet.label}</span>}
        <Address value={wallet.address} />
        {wallet.archived && <span className="badge">Archived</span>}
      </div>

      <div className="wallet-row-actions">
        {wallet.archived ? (
          <button
            type="button"
            onClick={() => {
              restoreMutation.mutate(wallet.id);
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
              onClick={() => {
                archiveMutation.mutate(wallet.id, {
                  onSuccess: () => {
                    setConfirming(false);
                  },
                });
              }}
              disabled={archiveMutation.isPending}
            >
              Confirm archive
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
              disabled={archiveMutation.isPending}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => {
              setConfirming(true);
            }}
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
 */
export function WalletList() {
  const [includeArchived, setIncludeArchived] = useState(false);
  const wallets = useWallets(includeArchived);

  return (
    <section aria-labelledby="wallet-list-heading">
      <div className="wallet-list-header">
        <h3 id="wallet-list-heading">Your wallets</h3>
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
        />
      )}

      {wallets.isSuccess && wallets.data.length === 0 && (
        <EmptyState
          title="No wallets yet"
          description="Add a wallet above to start tracking its balance and value."
        />
      )}

      {wallets.isSuccess && wallets.data.length > 0 && (
        <ul className="wallet-list">
          {wallets.data.map((wallet) => (
            <WalletRow key={wallet.id} wallet={wallet} />
          ))}
        </ul>
      )}
    </section>
  );
}
