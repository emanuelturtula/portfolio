import { describeApiError } from '@/api/client';
import { useExportReminder, useMarkExportMonthDone } from '@/api/exports';
import { formatMonth, listExchanges } from '@/lib/exports';

const FAILURE_FALLBACK = 'The server could not be reached.';

/**
 * The monthly reminder to export each exchange's transactions by hand (spec 040).
 *
 * From the 1st of each month, Argentina time, the month before is owed until the owner marks
 * it done here. Every owed month gets its own line and its own "Mark as done", oldest first,
 * so a month skipped stays on screen rather than being folded into the next one.
 *
 * Nothing renders while the request is pending, or when nothing is owed. A failed check is
 * said in one line rather than shown as nothing, because "nothing owed" and "could not tell"
 * call for different things from the owner. The test is on `data`, so a poll that fails after
 * the reminder was shown leaves it on screen.
 */
export function ExportReminder() {
  const reminder = useExportReminder();
  const markDone = useMarkExportMonthDone();

  if (reminder.data === undefined) {
    return reminder.isError ? (
      <p className="note note-error" role="alert">
        Could not check whether the monthly exchange exports are pending:{' '}
        {describeApiError(reminder.error, FAILURE_FALLBACK)}
      </p>
    ) : null;
  }

  const { months, exchanges } = reminder.data;
  if (months.length === 0) {
    return null;
  }

  const sources = listExchanges(exchanges);
  return (
    <section className="note-warning export-reminder" aria-labelledby="export-reminder-heading">
      <strong id="export-reminder-heading">Monthly exports pending</strong>
      <ul>
        {months.map((month) => {
          const label = formatMonth(month);
          return (
            <li key={month}>
              <span>
                Download the {label} transactions from {sources}, and save them to your Drive.
              </span>
              <button
                type="button"
                onClick={() => {
                  markDone.mutate(month);
                }}
                disabled={markDone.isPending}
                aria-label={`Mark ${label} as done`}
              >
                Mark as done
              </button>
            </li>
          );
        })}
      </ul>
      {markDone.isError && (
        <p className="note note-error" role="alert">
          Could not mark {formatMonth(markDone.variables)} as done:{' '}
          {describeApiError(markDone.error, FAILURE_FALLBACK)}
        </p>
      )}
    </section>
  );
}
