import type { ReactElement } from 'react';

import { AbsoluteTime } from '@/components/AbsoluteTime';
import {
  describeBalancesFailure,
  describeChainFailed,
  describeNeverRead,
  describeNotReadYet,
  describeOutOfDate,
  describeStaleWallets,
  describeSyncFailed,
  describeUnreadWallets,
  formatVenues,
  type MissingSource,
} from '@/lib/accounting';

function MissingSourceNotice({ source }: { readonly source: MissingSource }): ReactElement {
  switch (source.kind) {
    case 'read_failed':
      return (
        <p role="alert">
          {describeBalancesFailure(source.venue, source.error)}{' '}
          {source.lastReadAt === null ? (
            describeNeverRead(source.venue)
          ) : (
            <>
              Its last good reading, from <AbsoluteTime value={source.lastReadAt} />, is not used,
              so the coins held there are left out of the comparison.
            </>
          )}
        </p>
      );
    case 'never_read':
      return <p role="alert">{describeNotReadYet(source.venue)}</p>;
    case 'sync_failed':
      return (
        <p role="alert">
          {describeSyncFailed(source.venue)} They were last read on{' '}
          <AbsoluteTime value={source.lastReadAt} />.
        </p>
      );
    case 'out_of_date':
      return (
        <p role="alert">
          The balances at {formatVenues([source.venue])} were last read on{' '}
          <AbsoluteTime value={source.lastReadAt} />. {describeOutOfDate(source.maxAgeHours)}
        </p>
      );
    case 'wallets_chain_failed':
      return <p role="alert">{describeChainFailed(source.chain, source.count)}</p>;
    case 'wallets_stale':
      return <p role="alert">{describeStaleWallets(source.count, source.maxAgeHours)}</p>;
    case 'wallets_unread':
      return <p role="alert">{describeUnreadWallets(source.count)}</p>;
  }
}

/**
 * One key per notice: a venue has at most one, a chain has at most one, and each of the other
 * wallet notices is its own kind.
 */
function noticeKey(source: MissingSource): string {
  if ('venue' in source) {
    return `${source.kind}-${source.venue}`;
  }

  return 'chain' in source ? `${source.kind}-${source.chain}` : source.kind;
}

/**
 * The sources left out of the comparison, one `role="alert"` paragraph each, so a screen
 * reader hears that coins are not in it. A source that is left out contributes nothing - never
 * a stale reading that could count coins twice - and the paragraph says which one it is and
 * why, since a missing source can hide a real "held exceeds history". Every instant shown is
 * absolute, never a `<RelativeTime>`: a ticking phrase inside a live region is re-announced
 * every time it changes (spec 016).
 */
export function MissingSources({ sources }: { readonly sources: readonly MissingSource[] }) {
  return (
    <>
      {sources.map((source) => (
        <MissingSourceNotice key={noticeKey(source)} source={source} />
      ))}
    </>
  );
}
