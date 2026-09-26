import type { Exchange } from '@/api/exchanges';
import { EXCHANGES, formatCount } from '@/lib/exchanges';
import { formatAbsoluteTime, formatHistoryStart } from '@/lib/time';

interface TruncationBannerProps {
  /**
   * `effective_since` is `string` here, not the `string | null` `Exchange` itself declares -
   * the caller (`ExchangesPage`) only ever constructs this prop through `isTruncated`'s type
   * guard, which has already ruled `null` out. That is deliberate: a second `null` check in
   * here, after the caller's filter already made it impossible, is exactly the kind of
   * branch no fixture can ever exercise (a prior version had one; see the spec's "What the
   * backend can write" and this component's history).
   */
  readonly exchange: Exchange & { readonly effective_since: string };
}

/**
 * The warning banner for one venue whose retention window cut its imported history short
 * (spec criterion 5).
 *
 * Deliberately not a live region: this is page state a 5-second poll would otherwise
 * re-announce on every tick while a sync is running, not a transition to interrupt for.
 */
export function TruncationBanner({ exchange }: TruncationBannerProps) {
  const {
    effective_since: effectiveSince,
    exchange_key: exchangeKey,
    pending_windows: pendingWindows,
  } = exchange;

  const venue = EXCHANGES[exchangeKey].name;
  const headingId = `truncation-heading-${exchangeKey}`;

  return (
    <section className="state state-warning" aria-labelledby={headingId}>
      <h3 id={headingId}>{venue} history is incomplete</h3>
      <p>
        {venue} does not return trades older than its retention window, so the history imported here
        is complete only from{' '}
        <time dateTime={effectiveSince} title={formatAbsoluteTime(effectiveSince)}>
          <strong>{formatHistoryStart(effectiveSince)}</strong>
        </time>
        . Any trade made before then may be missing.
      </p>
      {pendingWindows > 0 && (
        <p>
          The import has not finished. That is where the history will be complete from once it does
          ({formatCount(pendingWindows)} window{pendingWindows === 1 ? '' : 's'} still to read).
        </p>
      )}
    </section>
  );
}
