import { useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import type { Adjustment, DeleteOutcome } from '@/api/adjustments';
import { AdjustmentForm } from '@/pages/adjustments/AdjustmentForm';
import { AdjustmentList } from '@/pages/adjustments/AdjustmentList';
import { FailedRecomputeAlert } from '@/pages/adjustments/FailedRecomputeAlert';

/**
 * What the status line says when a delete has left the adjustment gone. "Already deleted" is
 * the `404`: it was removed elsewhere before this request, and the owner is told so, not left
 * with a row that vanished in silence.
 */
const DELETE_MESSAGES: Record<DeleteOutcome, string> = {
  deleted: 'Adjustment deleted.',
  already_deleted: 'That adjustment was already deleted.',
};

/**
 * What the form is showing, and the `key` that remounts it. Every change of mode, and every
 * reset to empty, is a new `nonce`: the form starts each of its fields from `useState`, so
 * the way to fill it from an adjustment or to empty it is a new instance, not an effect that
 * copies props into state.
 */
interface FormTarget {
  /** The adjustment being edited, or `null` for the create form. */
  readonly adjustment: Adjustment | null;
  readonly nonce: number;
  /** Whether the new form takes focus on its heading. See `AdjustmentFormProps`. */
  readonly focusHeading: boolean;
}

/**
 * The manual adjustments: record, edit and delete the coins the imported history does not
 * show. See docs/specs/027-manual-adjustments-page.md; what an adjustment is, and every rule
 * for an acceptable one, is spec 023's and the server's.
 *
 * The form and the list are independent components on purpose - saving needs nothing the list
 * provides, and the list needs nothing from the form - so a list that is loading or has
 * failed never takes the form down. What they share lives here: which adjustment the form is
 * editing, and the one line that says what just happened - a single `role="status"` paragraph
 * that is always in the document, whose text is swapped, because a live region that is inserted
 * together with its own text is not reliably announced (spec 016, R11).
 *
 * `/adjustments?asset=BTC` opens the create form with the asset filled in, which is where the
 * holdings check sends the owner. Nothing else is read from the URL, and only the first form
 * uses it: after a save or a cancel the form is empty, as it was promised to be.
 */
export function AdjustmentsPage() {
  const [searchParams] = useSearchParams();
  const [urlAsset] = useState(() => searchParams.get('asset') ?? '');
  const [target, setTarget] = useState<FormTarget>({
    adjustment: null,
    nonce: 0,
    focusHeading: false,
  });
  const [status, setStatus] = useState<string | null>(null);
  const formAreaRef = useRef<HTMLDivElement | null>(null);

  /** The last outcome is about to be replaced by the next attempt's, whichever it is. */
  function forgetStatus() {
    setStatus(null);
  }

  function showForm(adjustment: Adjustment | null, focusHeading: boolean) {
    setTarget((current) => ({ adjustment, nonce: current.nonce + 1, focusHeading }));
  }

  return (
    <section className="page" aria-labelledby="adjustments-heading">
      <div className="page-head">
        <h2 id="adjustments-heading" className="page-title">
          Adjustments
        </h2>
        <p className="page-meta">Coins the imported history does not show.</p>
      </div>

      <FailedRecomputeAlert />

      <div ref={formAreaRef} className="card">
        <AdjustmentForm
          key={target.nonce}
          adjustment={target.adjustment}
          initialAsset={target.nonce === 0 ? urlAsset : ''}
          focusHeading={target.focusHeading}
          onAttempt={forgetStatus}
          onSaved={(message) => {
            setStatus(message);
            showForm(null, true);
          }}
          onCancel={() => {
            showForm(null, true);
          }}
        />
      </div>

      <p role="status" className="status-line">
        {status}
      </p>

      <AdjustmentList
        onEdit={(adjustment) => {
          forgetStatus();
          showForm(adjustment, true);
        }}
        onAttempt={forgetStatus}
        onDeleted={(id, outcome) => {
          setStatus(DELETE_MESSAGES[outcome]);
          // Read here and not inside the updater below, which must be pure and which React
          // may run twice. Focus in the form means the owner has gone on typing while the
          // delete was in flight: the field that has it is about to be replaced, so the new
          // form takes it on its heading. Anywhere else, the delete hands focus to the list,
          // where the row was, and the form takes none.
          const focusWasInForm = formAreaRef.current?.contains(document.activeElement) === true;
          // Only the form that is editing the deleted adjustment is emptied.
          setTarget((current) =>
            current.adjustment?.id === id
              ? { adjustment: null, nonce: current.nonce + 1, focusHeading: focusWasInForm }
              : current,
          );
        }}
      />
    </section>
  );
}
