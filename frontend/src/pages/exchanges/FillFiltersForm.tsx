import { EXCHANGES } from '@/lib/exchanges';
import {
  displayTimeZone,
  EXCHANGE_KEYS,
  hasActiveFilters,
  toggleExchange,
  writeFillFilters,
  type FillFilters,
} from '@/lib/fillFilters';
import { INVERTED_RANGE_MESSAGE } from '@/lib/fills';
import { DayInput } from '@/pages/exchanges/DayInput';

interface FillFiltersFormProps {
  readonly filters: FillFilters;
  /** The end day is before the start day: shown, and never sent. */
  readonly inverted: boolean;
  readonly onChange: (next: FillFilters) => void;
  readonly onClear: () => void;
}

/**
 * The exchange checkboxes and the two day pickers. There is no Apply button: every control
 * writes to the URL's filters and shows them, so the URL is the single source of truth and
 * back/forward restore exactly what the form shows. The day pickers keep a draft of what is
 * being typed until it is a day the filters may hold (see `DayInput`), and are told to drop
 * it whenever the filters change under them - `syncKey` is the filters, as the URL writes them.
 *
 * The venue choices are every `ExchangeKey`, not the ones the exchange list happens to hold,
 * so the form does not depend on that request. None checked is every exchange.
 *
 * **Clear filters is always there.** With nothing to clear it is `aria-disabled` and does
 * nothing, rather than absent or natively `disabled`: pressing it is what empties the filters
 * it exists to clear, and a button that unmounts, or is disabled, at that moment takes the
 * keyboard's focus to `<body>` (spec 016, R10).
 */
export function FillFiltersForm({ filters, inverted, onChange, onClear }: FillFiltersFormProps) {
  const active = hasActiveFilters(filters);
  const syncKey = writeFillFilters(filters, 1).toString();

  return (
    <div className="fill-filters" role="group" aria-label="Transaction filters">
      <fieldset>
        <legend>Exchanges</legend>
        {EXCHANGE_KEYS.map((key) => (
          <label key={key} className="fill-filter-choice">
            <input
              type="checkbox"
              checked={filters.exchanges.includes(key)}
              onChange={() => {
                onChange(toggleExchange(filters, key));
              }}
            />
            {EXCHANGES[key].name}
          </label>
        ))}
        <p className="hint">Leave all unchecked to include every exchange.</p>
      </fieldset>

      <DayInput
        id="fills-from"
        label="From"
        day={filters.fromDay}
        syncKey={syncKey}
        describedBy="fills-timezone"
        invalid={false}
        onDay={(fromDay) => {
          onChange({ ...filters, fromDay });
        }}
      />
      <DayInput
        id="fills-to"
        label="To"
        day={filters.toDay}
        syncKey={syncKey}
        describedBy="fills-timezone"
        invalid={inverted}
        onDay={(toDay) => {
          onChange({ ...filters, toDay });
        }}
      />

      <button
        type="button"
        className="fill-filters-clear"
        aria-disabled={active ? undefined : 'true'}
        onClick={() => {
          if (active) {
            onClear();
          }
        }}
      >
        Clear filters
      </button>

      <p id="fills-timezone" className="hint fill-filters-note">
        Dates are days in {displayTimeZone()}.
      </p>
      {inverted && (
        <p className="fill-filters-note state-error" role="alert">
          {INVERTED_RANGE_MESSAGE}
        </p>
      )}
    </div>
  );
}
