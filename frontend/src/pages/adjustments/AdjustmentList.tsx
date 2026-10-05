import { useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import {
  useAdjustments,
  useDeleteAdjustment,
  type Adjustment,
  type DeleteOutcome,
} from '@/api/adjustments';
import { describeApiError } from '@/api/client';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Money } from '@/components/Money';
import { Skeleton } from '@/components/Skeleton';
import { UNIT_PRICE_FORMAT } from '@/lib/accounting';
import { adjustmentName } from '@/lib/adjustments';
import { money } from '@/lib/money';

const LOAD_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const REFETCH_FALLBACK = 'The server could not be reached.';
const DELETE_FALLBACK = 'Could not delete the adjustment. Try again.';

const LIST_HEADING_ID = 'adjustment-list-heading';

/**
 * The text where a cost would be when there is none. An unknown cost is not a zero cost and is
 * not a dash: it is the fact that the units count toward the quantity held and are left out of
 * the average cost.
 */
export const UNKNOWN_COST = 'Unknown cost';

interface AdjustmentRowProps {
  readonly adjustment: Adjustment;
  readonly onEdit: (adjustment: Adjustment) => void;
  readonly onAttempt: () => void;
  readonly onDeleted: (id: number, outcome: DeleteOutcome) => void;
  /** Focuses the list's own heading, the one element still on screen once a row has gone. */
  readonly focusListHeading: () => void;
}

/**
 * One row: its figures, and its Edit and Delete controls.
 *
 * **Delete asks first.** The button is replaced by "Confirm delete" and "Cancel", and focus
 * follows: to the confirm button when it appears, back to Delete on Cancel. `flushSync` applies
 * the state change before the ref is read, because the button being focused has not been
 * mounted until it has. No pressed control may vanish without handing focus to whatever
 * replaces it.
 *
 * **A confirmed delete removes this row**, so what follows it cannot live here: the hook's own
 * `onGone` (see `useDeleteAdjustment`) hands the id and the outcome to the page, which says what
 * happened and empties the form if it was editing this adjustment, and focus moves to the
 * list's heading - but only if it is still this row's to move. The outcome is "deleted", or
 * "already deleted" when the API answered that the adjustment was not there: either way it is
 * gone, which is what was asked, and there is one road for both. Focus on `<body>` (the row is
 * already gone) or somewhere inside the row (it is about to go) is the owner still attending to
 * this delete; focus anywhere else means they have moved on, to the form for one, and dragging
 * it back would lose what they are doing there. (When the form was editing this very
 * adjustment, the page emptied it, and gives the new form's heading the focus the field it
 * held has lost.)
 */
function AdjustmentRow({
  adjustment,
  onEdit,
  onAttempt,
  onDeleted,
  focusListHeading,
}: AdjustmentRowProps) {
  const [confirming, setConfirming] = useState(false);
  const rowRef = useRef<HTMLTableRowElement | null>(null);
  const deleteButtonRef = useRef<HTMLButtonElement | null>(null);
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null);
  const name = adjustmentName(adjustment);

  function ownsFocus(): boolean {
    const active = document.activeElement;
    return active === document.body || (rowRef.current?.contains(active) ?? false);
  }

  const deleteMutation = useDeleteAdjustment((outcome) => {
    // Asked before anything is changed, while the confirm button still holds focus if the
    // row is still on screen.
    const attending = ownsFocus();
    // For a list that could not be read again afterwards: the row is still there, and it
    // must not stay stuck on a confirmation of something already done.
    setConfirming(false);
    if (attending) {
      focusListHeading();
    }
    onDeleted(adjustment.id, outcome);
  });

  function openConfirm() {
    flushSync(() => {
      setConfirming(true);
    });
    confirmButtonRef.current?.focus();
  }

  function cancelConfirm() {
    // Also forgets a failed attempt: the owner has decided not to delete, and the alert
    // about the attempt they have abandoned would otherwise outlive the decision.
    deleteMutation.reset();
    flushSync(() => {
      setConfirming(false);
    });
    deleteButtonRef.current?.focus();
  }

  return (
    <tr ref={rowRef}>
      <th scope="row">{adjustment.asset}</th>
      <td className="num">
        <Money value={money(adjustment.quantity)} />
      </td>
      <td className="num">
        {adjustment.unit_cost === null ? (
          UNKNOWN_COST
        ) : (
          <Money value={money(adjustment.unit_cost)} options={UNIT_PRICE_FORMAT} />
        )}
      </td>
      <td>
        <AbsoluteTime value={adjustment.occurred_at} />
      </td>
      <td className="note">{adjustment.note}</td>
      <td>
        <div className="row-actions">
          <button
            type="button"
            aria-label={`Edit ${name}`}
            onClick={() => {
              onEdit(adjustment);
            }}
          >
            Edit
          </button>
          {confirming ? (
            <>
              <button
                type="button"
                ref={confirmButtonRef}
                aria-label={`Confirm delete of ${name}`}
                onClick={() => {
                  onAttempt();
                  deleteMutation.mutate(adjustment.id);
                }}
                disabled={deleteMutation.isPending}
              >
                Confirm delete
              </button>
              <button
                type="button"
                aria-label={`Cancel deleting ${name}`}
                onClick={cancelConfirm}
                disabled={deleteMutation.isPending}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              ref={deleteButtonRef}
              aria-label={`Delete ${name}`}
              onClick={openConfirm}
            >
              Delete
            </button>
          )}
        </div>
        {deleteMutation.isError && (
          <p className="field-error" role="alert">
            {describeApiError(deleteMutation.error, DELETE_FALLBACK)}
          </p>
        )}
      </td>
    </tr>
  );
}

interface AdjustmentListProps {
  /** Starts editing `adjustment`: the page fills the form and moves focus to it. */
  readonly onEdit: (adjustment: Adjustment) => void;
  /** Called when "Confirm delete" is pressed, before anything is sent: the page forgets the
   * last outcome it reported, which this attempt is about to replace. */
  readonly onAttempt: () => void;
  /**
   * An adjustment is gone, deleted by this request or already gone before it. The page says
   * which, and empties the form if it was editing it.
   */
  readonly onDeleted: (id: number, outcome: DeleteOutcome) => void;
}

/**
 * The recorded adjustments, with their own loading, empty, error and success states -
 * independent of the form, so a list that is loading or has failed never takes the form down
 * with it.
 *
 * The four states are told apart, and the two that mean opposite things are never the same:
 * "No adjustments yet" is an honest absence, and a list that could not be read is an error
 * with a retry. A refetch that fails after a good load keeps the rows - the last reading is
 * still a reading - and says above them that it is stale.
 *
 * In the endpoint's order, which is the order they replay in: by `occurred_at`, then id. Every
 * figure is a `<data>` element holding the string as it was sent, and a cost that is `null` is
 * the words "Unknown cost". The table sits in a focusable, labelled scroll region like the
 * other tables, so a phone scrolls the table and never the page.
 */
export function AdjustmentList({ onEdit, onAttempt, onDeleted }: AdjustmentListProps) {
  const adjustments = useAdjustments();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const data = adjustments.data;

  function focusListHeading() {
    headingRef.current?.focus();
  }

  return (
    <div className="card">
      <h3 id={LIST_HEADING_ID} ref={headingRef} tabIndex={-1}>
        Recorded adjustments
      </h3>

      {adjustments.isPending && <Skeleton label="Loading adjustments…" />}

      {adjustments.isLoadingError && (
        <ErrorState
          headingLevel={4}
          title="Could not load the adjustments"
          description={describeApiError(adjustments.error, LOAD_FALLBACK)}
          onRetry={() => {
            void adjustments.refetch();
          }}
        />
      )}

      {adjustments.isRefetchError && (
        <p role="alert">
          Could not refresh the adjustments: {describeApiError(adjustments.error, REFETCH_FALLBACK)}{' '}
          Showing what was last loaded.
        </p>
      )}

      {data?.length === 0 && (
        <EmptyState
          headingLevel={4}
          title="No adjustments yet"
          description="Use the form above to record coins the imported history does not show."
        />
      )}

      {data !== undefined && data.length > 0 && (
        <div className="table-scroll" role="region" aria-labelledby={LIST_HEADING_ID} tabIndex={0}>
          <table className="data-table data-table--sticky adjustment-table">
            <thead>
              <tr>
                <th scope="col">Asset</th>
                <th scope="col" className="num">
                  Quantity
                </th>
                <th scope="col" className="num">
                  Unit cost (USD)
                </th>
                <th scope="col">Acquired</th>
                <th scope="col">Note</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.map((adjustment) => (
                <AdjustmentRow
                  key={adjustment.id}
                  adjustment={adjustment}
                  onEdit={onEdit}
                  onAttempt={onAttempt}
                  onDeleted={onDeleted}
                  focusListHeading={focusListHeading}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
