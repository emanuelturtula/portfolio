import { EXCHANGES, type ExchangeKey } from '@/lib/exchanges';

interface FailingAccountAlertsProps {
  /** The venues whose last sync failed (`error` or `auth_failed`), in the list's order. */
  readonly venues: readonly ExchangeKey[];
}

/**
 * One alert line per failing account, above the transactions. The accounts sit below the
 * table now, and a failing sync is the one thing on this page the owner must not have to
 * scroll to find. Each line names the venue and links to its entry (`#exchange-<key>`).
 *
 * A plain anchor, not a router `<Link to="#exchange-...">`: a link with only a hash resolves
 * to a location with no search, and would drop the transaction filters held in the URL.
 */
export function FailingAccountAlerts({ venues }: FailingAccountAlertsProps) {
  return (
    <>
      {venues.map((venue) => {
        const name = EXCHANGES[venue].name;
        return (
          <p key={venue} className="state-error" role="alert">
            {name}: the last sync failed. <a href={`#exchange-${venue}`}>See the {name} account</a>.
          </p>
        );
      })}
    </>
  );
}
