import type { Exchange } from '@/api/exchanges';
import { completenessNotes } from '@/lib/fills';
import type { FillFilters } from '@/lib/fillFilters';

interface CompletenessNoticeProps {
  /** `undefined` is an exchange list that could not be read. */
  readonly exchanges: readonly Exchange[] | undefined;
  readonly filters: FillFilters;
}

/**
 * Why the totals may cover less than the venues hold: a history a retention window cut short,
 * an import still running, a sync that is failing, or an exchange list that could not be read
 * at all. One sentence per selected venue and reason, and nothing when there is nothing to say.
 *
 * Totals cover only what has been imported, and a partial history read as complete is the
 * failure this exists to prevent. Not a live region: it is page state, and the lists behind
 * it are polled, so a region would announce it again on every change of a venue.
 */
export function CompletenessNotice({ exchanges, filters }: CompletenessNoticeProps) {
  const notes = completenessNotes(exchanges, filters);

  if (notes.length === 0) {
    return null;
  }

  return (
    <div className="state-warning completeness" role="note" aria-label="Completeness">
      <p>
        <strong>This history may be incomplete.</strong>
      </p>
      <ul>
        {notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </div>
  );
}
