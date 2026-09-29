import { useState } from 'react';

import { dayFromInput, isSelectableDay, MAX_DAY, MIN_DAY } from '@/lib/fillFilters';

interface DayInputProps {
  readonly id: string;
  readonly label: string;
  /** The day the URL holds. */
  readonly day: string | null;
  /**
   * Changes whenever the filters do. It is what tells this input that the URL moved on
   * without it: Clear filters, back/forward, another control's change.
   */
  readonly syncKey: string;
  readonly describedBy: string;
  readonly invalid: boolean;
  readonly onDay: (day: string | null) => void;
}

/**
 * A date input that shows what the owner is typing, and tells the URL only when that is a day
 * the filters may hold.
 *
 * **Why a draft.** Typing a year by keyboard passes through 0002, 0020 and 0202 on the way to
 * 2026, and the browser reports each as a real date. The filters refuse a day outside
 * `MIN_DAY`..`MAX_DAY`, and an input driven by the URL alone would then be reset to empty
 * under the owner's fingers on the first digit. The draft is what the input shows, so it
 * keeps what was typed; the URL, and with it the request, only ever sees an empty field or a
 * selectable day.
 *
 * **Resync.** When `syncKey` changes the draft is replaced by the URL's day, using the
 * "adjust state while rendering" pattern rather than an effect, so there is no render in
 * between that shows the old text. Committing a day of one's own changes `syncKey` too, and
 * replaces the draft with the same text.
 */
export function DayInput({ id, label, day, syncKey, describedBy, invalid, onDay }: DayInputProps) {
  const [draft, setDraft] = useState(day ?? '');
  const [seenKey, setSeenKey] = useState(syncKey);

  if (seenKey !== syncKey) {
    setSeenKey(syncKey);
    setDraft(day ?? '');
  }

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="date"
        min={MIN_DAY}
        max={MAX_DAY}
        value={draft}
        aria-describedby={describedBy}
        aria-invalid={invalid}
        onChange={(event) => {
          const typed = event.target.value;
          setDraft(typed);
          if (typed === '' || isSelectableDay(typed)) {
            onDay(dayFromInput(typed));
          }
        }}
      />
    </div>
  );
}
