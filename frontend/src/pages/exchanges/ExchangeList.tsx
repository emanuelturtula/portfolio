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
}

/**
 * The ordered remediation steps for an `auth_failed` account (spec criterion 2), chosen by
 * `remediationFor`. Returns nothing for any other status - `remediationFor` already encodes
 * that this is the only status that needs the owner to act.
 */
function Remediation({ exchange }: ExchangeRowProps) {
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
            Recreate the container with <code>docker compose up --force-recreate</code>. A restart
            does not re-read <code>secrets.env</code>.
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
            <p>A new key instead needs steps 2 and 3 above first.</p>
          </li>
        </ol>
      )}
      <p>docs/operations.md, section 13, has the full procedure.</p>
    </>
  );
}

/**
 * One venue's entry: its status, what it holds, and whichever of the spec's seven messages
 * apply, in order. Several can apply at once - a `syncing` `auth_failed` venue still shows
 * its error and its remediation, because until the run ends they are still true.
 */
function ExchangeRow({ exchange }: ExchangeRowProps) {
  const venue = EXCHANGES[exchange.exchange_key].name;

  return (
    <li aria-label={venue}>
      <p>
        <strong>{venue}</strong> - {statusLabel(exchange)}
      </p>

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
          {exchange.effective_since === null
            ? 'Not planned yet'
            : formatHistoryStart(exchange.effective_since)}
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

      {exchange.syncing && <p>A sync is reading {venue} now.</p>}

      {exchange.last_error !== null && (
        <p>
          {errorSentence(exchange.last_error.error_kind, venue)}
          {exchange.last_error.detail !== null && <> Detail: {exchange.last_error.detail}</>}
        </p>
      )}

      <Remediation exchange={exchange} />

      {exchange.status === 'error' && exchange.configured && !exchange.syncing && (
        <p>The next scheduled sync tries again.</p>
      )}

      {exchange.pending_windows > 0 && exchange.configured && !exchange.syncing && (
        <p>
          {exchange.pending_windows === 1 ? (
            <>1 window of history is still to read. The next sync continues from it.</>
          ) : (
            <>
              {formatCount(exchange.pending_windows)} windows of history are still to read. The next
              sync continues from them.
            </>
          )}
        </p>
      )}

      {exchange.status === 'never_synced' && exchange.configured && !exchange.syncing && (
        <p>
          No sync has finished for {venue} yet. The next scheduled sync imports its history, or
          press Sync now.
        </p>
      )}
    </li>
  );
}

interface ExchangeListProps {
  readonly exchanges: readonly Exchange[];
}

/** The Accounts section: one list item per venue, each labelled by its venue name. */
export function ExchangeList({ exchanges }: ExchangeListProps) {
  return (
    <section aria-labelledby="exchanges-accounts-heading">
      <h3 id="exchanges-accounts-heading">Accounts</h3>
      <ul className="exchange-list">
        {exchanges.map((exchange) => (
          <ExchangeRow key={exchange.exchange_key} exchange={exchange} />
        ))}
      </ul>
    </section>
  );
}
