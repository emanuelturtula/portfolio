import type { Exchange } from '@/api/exchanges';
import { RelativeTime } from '@/components/RelativeTime';
import {
  EXCHANGES,
  errorSentence,
  formatCount,
  remediationFor,
  statusLabel,
} from '@/lib/exchanges';
import { formatHistoryStart } from '@/lib/time';

interface ExchangeRowProps {
  readonly exchange: Exchange;
  /**
   * Whether the run log's newest run is `running` with `trigger: 'manual'` - i.e. an owner
   * really is retrying `auth_failed` accounts right now, as opposed to a scheduled or
   * startup run merely skipping them (spec R17). Computed once by `ExchangesPage` from the
   * run log, not from this page's own pending `POST`: a request that *joined* a scheduled
   * run is pending too, and that run still skips the account.
   */
  readonly manualRunInFlight: boolean;
}

interface RemediationProps {
  readonly exchange: Exchange;
}

/**
 * The ordered remediation steps for an `auth_failed` account (spec criterion 2), chosen by
 * `remediationFor`. Returns nothing for any other status - `remediationFor` already encodes
 * that this is the only status that needs the owner to act. The caller hides this component
 * entirely during a manual retry in flight (spec R17) - see `ExchangeRow`.
 */
function Remediation({ exchange }: RemediationProps) {
  const kind = remediationFor(exchange);
  if (kind === null) {
    return null;
  }

  const venue = EXCHANGES[exchange.exchange_key].name;
  const variables = EXCHANGES[exchange.exchange_key].variables;

  return (
    <>
      {kind === 'key' ? (
        <ol>
          <li>
            At {venue}, check that the API key still exists, or create a new{' '}
            <strong>read-only</strong> key. If the key has an IP allowlist, it must include the
            address the host reaches the internet from.
          </li>
          <li>
            Put the values in <code>secrets.env</code> on the host:{' '}
            {variables.map((variable, index) => (
              <span key={variable}>
                {index > 0 && ', '}
                <code>{variable}</code>
              </span>
            ))}
            .
          </li>
          <li>
            Recreate the container with{' '}
            <code>~/portfolio-app/prod/compose.sh up -d --force-recreate app</code>. A restart does
            not re-read <code>secrets.env</code>.
          </li>
          <li>
            Press <strong>Sync now</strong>. Scheduled syncs skip {venue} until a sync you start
            succeeds.
          </li>
        </ol>
      ) : (
        <ol>
          <li>
            At {venue}, edit the API key and grant <strong>read</strong> permission. Grant nothing
            else, and never trade, transfer or withdrawal.
          </li>
          <li>
            Press <strong>Sync now</strong>. Scheduled syncs skip {venue} until a sync you start
            succeeds.
            <p>
              A new key must first go into <code>secrets.env</code> on the host (
              {variables.map((variable, index) => (
                <span key={variable}>
                  {index > 0 && ', '}
                  <code>{variable}</code>
                </span>
              ))}
              ), and the container be recreated with{' '}
              <code>~/portfolio-app/prod/compose.sh up -d --force-recreate app</code>.
            </p>
          </li>
        </ol>
      )}
      <p>docs/operations.md, section 13, has the full procedure.</p>
    </>
  );
}

/**
 * Rule 6's sentence, gated by the caller on `pending_windows > 0 && configured && !syncing`.
 * `auth_failed` gets its own wording (spec R2): a scheduled run skips that account, so "the
 * next sync continues from them" is false for it - only a sync the owner starts does.
 */
function windowsPendingSentence(pendingWindows: number, authFailed: boolean): string {
  const noun = pendingWindows === 1 ? 'window' : 'windows';
  const verb = pendingWindows === 1 ? 'is' : 'are';
  const pronoun = pendingWindows === 1 ? 'it' : 'them';
  const count = formatCount(pendingWindows);

  if (authFailed) {
    return (
      `${count} ${noun} of history ${verb} still to read. The first sync you start after ` +
      `fixing the key continues from ${pronoun}.`
    );
  }
  return `${count} ${noun} of history ${verb} still to read. The next sync continues from ${pronoun}.`;
}

/**
 * One venue's entry: its status, what it holds, and whichever of the spec's seven messages
 * apply, in order. Several can apply at once - a `syncing` `auth_failed` venue still shows
 * its error, because it is still the last attempted outcome.
 *
 * `lastErrorKind` is computed once, rather than read through `exchange.last_error?.error_kind`
 * at each use: rules 5 and 6 (spec R6, R16) need the same narrowed value message 3 already
 * establishes, and reusing it is what keeps their `!== 'conflict'` checks from being a
 * second, independently-null-checked read of a field the fixtures only ever leave null for
 * the statuses that never reach those rules in the first place.
 *
 * `isManualRetry` (spec R17) is the one case where `auth_failed` and `syncing` together mean
 * something better than "Authentication failed" and its remediation: a manual run is
 * actually retrying this venue right now, not merely leaving a scheduled run to skip it.
 */
function ExchangeRow({ exchange, manualRunInFlight }: ExchangeRowProps) {
  const venue = EXCHANGES[exchange.exchange_key].name;
  const headingId = `exchange-heading-${exchange.exchange_key}`;
  const lastErrorKind = exchange.last_error === null ? null : exchange.last_error.error_kind;
  const isManualRetry = exchange.status === 'auth_failed' && exchange.syncing && manualRunInFlight;

  return (
    <li aria-labelledby={headingId}>
      <h4 id={headingId}>{venue}</h4>
      <p>{statusLabel(exchange, manualRunInFlight)}</p>

      <ul>
        <li>
          Last complete sync:{' '}
          {exchange.last_synced_at === null ? (
            'Never'
          ) : (
            <RelativeTime value={exchange.last_synced_at} />
          )}
          .
        </li>
        <li>Fills stored: {formatCount(exchange.fills_stored)}.</li>
        <li>
          History complete from:{' '}
          {exchange.effective_since === null ? (
            'Not planned yet'
          ) : exchange.pending_windows > 0 ? (
            <>
              {formatHistoryStart(exchange.effective_since)} once the windows still to read are read
            </>
          ) : (
            formatHistoryStart(exchange.effective_since)
          )}
          .
        </li>
        {exchange.pending_windows > 0 && (
          <li>Windows still to read: {formatCount(exchange.pending_windows)}.</li>
        )}
      </ul>

      {!exchange.configured && (
        <p>
          No credentials for {venue} are configured on the host. The fills already imported are
          kept, and nothing new is read.
        </p>
      )}

      {exchange.syncing && (
        <p>
          {isManualRetry ? (
            <>This sync is retrying {venue}.</>
          ) : (
            <>
              A sync is running.
              {exchange.status === 'auth_failed' && (
                <> Only a sync you start retries {venue}; a scheduled one skips it.</>
              )}
            </>
          )}
        </p>
      )}

      {exchange.last_error !== null && (
        <p>
          {errorSentence(exchange.last_error.error_kind, venue)}
          {exchange.last_error.detail !== null && <> Detail: {exchange.last_error.detail}</>}
        </p>
      )}

      {!isManualRetry && <Remediation exchange={exchange} />}

      {exchange.status === 'error' &&
        exchange.configured &&
        !exchange.syncing &&
        lastErrorKind !== 'conflict' && <p>The next sync tries again.</p>}

      {exchange.pending_windows > 0 &&
        exchange.configured &&
        !exchange.syncing &&
        lastErrorKind !== 'conflict' && (
          <p>
            {windowsPendingSentence(exchange.pending_windows, exchange.status === 'auth_failed')}
          </p>
        )}

      {exchange.status === 'never_synced' && exchange.configured && !exchange.syncing && (
        <p>No sync has finished for {venue} yet. Press Sync now to start one.</p>
      )}
    </li>
  );
}

interface ExchangeListProps {
  readonly exchanges: readonly Exchange[];
  /** See `ExchangeRowProps.manualRunInFlight`; the same value for every row. */
  readonly manualRunInFlight: boolean;
}

/** The Accounts section: one list item per venue, each labelled by its venue name. */
export function ExchangeList({ exchanges, manualRunInFlight }: ExchangeListProps) {
  return (
    <section aria-labelledby="exchanges-accounts-heading">
      <h3 id="exchanges-accounts-heading">Accounts</h3>
      <ul className="exchange-list">
        {exchanges.map((exchange) => (
          <ExchangeRow
            key={exchange.exchange_key}
            exchange={exchange}
            manualRunInFlight={manualRunInFlight}
          />
        ))}
      </ul>
    </section>
  );
}
