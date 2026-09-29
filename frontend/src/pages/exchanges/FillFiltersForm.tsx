import { EXCHANGES } from '@/lib/exchanges';
import {
  dayFromInput,
  displayTimeZone,
  EXCHANGE_KEYS,
  hasActiveFilters,
  toggleExchange,
  type FillFilters,
} from '@/lib/fillFilters';
import { INVERTED_RANGE_MESSAGE } from '@/lib/fills';

interface FillFiltersFormProps {
  readonly filters: FillFilters;
  /** The end day is before the start day: shown, and never sent. */
  readonly inverted: boolean;
  readonly onChange: (next: FillFilters) => void;
  readonly onClear: () => void;
}

/**
 * The exchange checkboxes and the two day pickers. There is no Apply button: every control is
 * driven by the URL's filters and writes back to it, so the URL is the single source of truth
 * and back/forward restore exactly what the form shows.
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

      <div className="field">
        <label htmlFor="fills-from">From</label>
        <input
          id="fills-from"
          type="date"
          value={filters.fromDay ?? ''}
          aria-describedby="fills-timezone"
          onChange={(event) => {
            onChange({ ...filters, fromDay: dayFromInput(event.target.value) });
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="fills-to">To</label>
        <input
          id="fills-to"
          type="date"
          value={filters.toDay ?? ''}
          aria-describedby="fills-timezone"
          aria-invalid={inverted}
          onChange={(event) => {
            onChange({ ...filters, toDay: dayFromInput(event.target.value) });
          }}
        />
      </div>

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
