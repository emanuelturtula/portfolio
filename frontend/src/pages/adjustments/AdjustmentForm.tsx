import { type SubmitEvent, useEffect, useRef, useState } from 'react';

import {
  useCreateAdjustment,
  useFirstTrades,
  useReplaceAdjustment,
  type Adjustment,
} from '@/api/adjustments';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import {
  emptyFormValues,
  firstTradeOf,
  formValuesOf,
  mapAdjustmentError,
  NO_ERRORS,
  prepareSubmission,
  suggestedDate,
  type AdjustmentFormValues,
  type FieldErrors,
} from '@/lib/adjustments';
import { displayTimeZone } from '@/lib/fillFilters';

export const RECORDED_MESSAGE = 'Adjustment recorded.';
export const UPDATED_MESSAGE = 'Adjustment updated.';

const ASSET_LIST_ID = 'adjustment-assets';

/** Every field has a hint, so there is always one id to give, and the extra ones come and go. */
function describedBy(hint: string, ...others: (string | undefined)[]): string {
  return [hint, ...others.filter((id): id is string => id !== undefined)].join(' ');
}

interface AdjustmentFormProps {
  /** The adjustment being edited, or `null` for the create form. Fixed for this mount. */
  readonly adjustment: Adjustment | null;
  /** The asset the URL carried, for the create form's first mount. Empty otherwise. */
  readonly initialAsset: string;
  /**
   * Whether to move focus to the form's heading on mount. Entering edit mode needs it, and so
   * does every reset after a save or a cancel: the control that was pressed is gone, and focus
   * must land somewhere rather than on `<body>`. So does the reset after the adjustment being
   * edited is deleted, when focus was inside the form: the field that had it is gone. The first
   * render of the page does not, and neither does that reset when focus was elsewhere, where
   * it belongs to the list.
   */
  readonly focusHeading: boolean;
  /** Called when the owner submits, before anything is checked or sent. */
  readonly onAttempt: () => void;
  readonly onSaved: (message: string) => void;
  readonly onCancel: () => void;
}

/**
 * The form that records an adjustment and edits one. One form in one of two modes, fixed for
 * as long as it is mounted: the page gives it a new `key` to change mode or to empty it, which
 * is what lets every field start from `useState` and nothing here sync with a prop.
 *
 * **The server is the validator** (spec 023). The form refuses locally only what it cannot
 * send - see `prepareSubmission` - and shows the server's own sentence under the field it
 * names, so there is no second copy of a rule to drift from the first.
 *
 * **Independent of the list and of the positions**: saving needs nothing either provides.
 * `first-trades` only adds a hint, a button and a list of assets; while it is pending or has
 * failed the form is the same form without them.
 */
export function AdjustmentForm({
  adjustment,
  initialAsset,
  focusHeading,
  onAttempt,
  onSaved,
  onCancel,
}: AdjustmentFormProps) {
  // What the fields held when the form was mounted. The date is compared against it on submit
  // to tell whether the owner changed it, and not against a value worked out again then: the
  // browser's time zone can change between Edit and Save, and an untouched date would move.
  const [mounted] = useState<AdjustmentFormValues>(() =>
    adjustment === null ? emptyFormValues(initialAsset) : formValuesOf(adjustment),
  );
  const [values, setValues] = useState<AdjustmentFormValues>(mounted);
  const [localErrors, setLocalErrors] = useState<FieldErrors>(NO_ERRORS);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const dateInputRef = useRef<HTMLInputElement | null>(null);

  const firstTrades = useFirstTrades();
  const create = useCreateAdjustment();
  const replace = useReplaceAdjustment();
  const save = adjustment === null ? create : replace;

  useEffect(() => {
    if (focusHeading) {
      headingRef.current?.focus();
    }
  }, [focusHeading]);

  // Either the server's answer to the last attempt, or what the form itself refused. Never
  // both: a refusal resets the mutation first, see `clearAttempt`.
  const errors = save.isError ? mapAdjustmentError(save.error) : localErrors;

  // `data`, not `isSuccess`: the earliest trade of an asset only moves earlier when a sync
  // imports older fills, and a sync invalidates this query, so what a failed refetch leaves
  // behind is still true. While the first load is pending or has failed there is no data, and
  // so no hint, no button and no datalist.
  const knownAssets = firstTrades.data?.assets;
  const trade = firstTradeOf(firstTrades.data, values.asset);
  const suggestion =
    trade === undefined
      ? undefined
      : { asset: trade.asset, at: trade.first_trade_at, ...suggestedDate(trade.first_trade_at) };

  /**
   * Forgets the last attempt's errors, local and server. The mutation is reset only when it
   * has failed: resetting one that is pending detaches this form from it, and the callback
   * that returns the form to empty after a save would never run.
   */
  function clearAttempt() {
    setLocalErrors(NO_ERRORS);
    if (save.isError) {
      save.reset();
    }
  }

  function update(field: keyof AdjustmentFormValues, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
    // Otherwise an error from the previous attempt stays on screen, attached to a value the
    // owner has already changed, until they submit again.
    clearAttempt();
  }

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    onAttempt();
    clearAttempt();

    const submission = prepareSubmission(values, adjustment, mounted.occurredAt);
    if (!submission.ok) {
      setLocalErrors(submission.errors);
      return;
    }

    if (adjustment === null) {
      create.mutate(submission.body, {
        onSuccess: () => {
          onSaved(RECORDED_MESSAGE);
        },
      });
    } else {
      replace.mutate(
        { id: adjustment.id, body: submission.body },
        {
          onSuccess: () => {
            onSaved(UPDATED_MESSAGE);
          },
        },
      );
    }
  }

  return (
    <form
      className="adjustment-form"
      onSubmit={handleSubmit}
      noValidate
      aria-labelledby="adjustment-form-heading"
    >
      <h3 id="adjustment-form-heading" ref={headingRef} tabIndex={-1}>
        {adjustment === null ? 'Record an adjustment' : 'Edit adjustment'}
      </h3>

      <div className="field">
        <label htmlFor="adjustment-asset">Asset</label>
        <input
          id="adjustment-asset"
          type="text"
          list={ASSET_LIST_ID}
          autoCapitalize="characters"
          autoComplete="off"
          spellCheck={false}
          value={values.asset}
          aria-required="true"
          aria-invalid={errors.asset !== undefined ? true : undefined}
          aria-describedby={describedBy(
            'adjustment-asset-hint',
            errors.asset !== undefined ? 'adjustment-asset-error' : undefined,
          )}
          onChange={(event) => {
            update('asset', event.target.value);
          }}
        />
        {knownAssets !== undefined && (
          <datalist id={ASSET_LIST_ID}>
            {knownAssets.map((known) => (
              <option key={known.asset} value={known.asset} />
            ))}
          </datalist>
        )}
        <p id="adjustment-asset-hint" className="hint">
          The symbol exactly as the exchanges spell it, in upper case, such as BTC.
        </p>
        {errors.asset !== undefined && (
          <p id="adjustment-asset-error" className="field-error" role="alert">
            {errors.asset}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="adjustment-quantity">Quantity</label>
        <input
          id="adjustment-quantity"
          type="text"
          autoComplete="off"
          value={values.quantity}
          aria-required="true"
          aria-invalid={errors.quantity !== undefined ? true : undefined}
          aria-describedby={describedBy(
            'adjustment-quantity-hint',
            errors.quantity !== undefined ? 'adjustment-quantity-error' : undefined,
          )}
          onChange={(event) => {
            update('quantity', event.target.value);
          }}
        />
        <p id="adjustment-quantity-hint" className="hint">
          Use a dot for the decimals, such as 0.5.
        </p>
        {errors.quantity !== undefined && (
          <p id="adjustment-quantity-error" className="field-error" role="alert">
            {errors.quantity}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="adjustment-unit-cost">Unit cost (USD)</label>
        <input
          id="adjustment-unit-cost"
          type="text"
          autoComplete="off"
          value={values.unitCost}
          aria-invalid={errors.unitCost !== undefined ? true : undefined}
          aria-describedby={describedBy(
            'adjustment-unit-cost-hint',
            errors.unitCost !== undefined ? 'adjustment-unit-cost-error' : undefined,
          )}
          onChange={(event) => {
            update('unitCost', event.target.value);
          }}
        />
        <p id="adjustment-unit-cost-hint" className="hint">
          Use a dot for the decimals. Leave empty if unknown. An unknown cost is not zero: the units
          count toward the quantity held and are left out of the average cost.
        </p>
        {errors.unitCost !== undefined && (
          <p id="adjustment-unit-cost-error" className="field-error" role="alert">
            {errors.unitCost}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="adjustment-occurred-at">Acquired on</label>
        <input
          id="adjustment-occurred-at"
          ref={dateInputRef}
          type="datetime-local"
          value={values.occurredAt}
          aria-required="true"
          aria-invalid={errors.occurredAt !== undefined ? true : undefined}
          aria-describedby={describedBy(
            'adjustment-occurred-at-hint',
            suggestion !== undefined ? 'adjustment-occurred-at-suggestion' : undefined,
            errors.occurredAt !== undefined ? 'adjustment-occurred-at-error' : undefined,
          )}
          onChange={(event) => {
            update('occurredAt', event.target.value);
          }}
        />
        <p id="adjustment-occurred-at-hint" className="hint">
          In your local time ({displayTimeZone()}).
        </p>
        {suggestion !== undefined && (
          <>
            <p id="adjustment-occurred-at-suggestion" className="hint">
              The earliest imported trade of {suggestion.asset} is{' '}
              <AbsoluteTime value={suggestion.at} />. Coins you already held by then should be dated
              before it; coins acquired later should carry the date you acquired them.
            </p>
            {/* Under the hint and at the app's regular button size, not inside the paragraph: a
                button the size of a line of hint text is a small target on a phone. */}
            <button
              type="button"
              className="suggestion-button"
              onClick={() => {
                update('occurredAt', suggestion.value);
                // The owner is dating this adjustment, so focus goes to the field they can
                // now adjust, not to the button that vanishes if the asset changes.
                dateInputRef.current?.focus();
              }}
            >
              Use {suggestion.label}
            </button>
          </>
        )}
        {errors.occurredAt !== undefined && (
          <p id="adjustment-occurred-at-error" className="field-error" role="alert">
            {errors.occurredAt}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="adjustment-note">Note</label>
        <textarea
          id="adjustment-note"
          rows={3}
          value={values.note}
          aria-required="true"
          aria-invalid={errors.note !== undefined ? true : undefined}
          aria-describedby={describedBy(
            'adjustment-note-hint',
            errors.note !== undefined ? 'adjustment-note-error' : undefined,
          )}
          onChange={(event) => {
            update('note', event.target.value);
          }}
        />
        <p id="adjustment-note-hint" className="hint">
          Why you are recording this, in your words.
        </p>
        {errors.note !== undefined && (
          <p id="adjustment-note-error" className="field-error" role="alert">
            {errors.note}
          </p>
        )}
      </div>

      {errors.form?.map((message, index) => (
        // Keyed by position, not by `message`: two entries can carry the same text, and a
        // duplicate key would be a React warning at best and a misrendered list at worst.
        <p key={index} className="field-error" role="alert">
          {message}
        </p>
      ))}

      <div className="form-actions">
        <button type="submit" disabled={save.isPending}>
          {adjustment === null ? 'Record adjustment' : 'Save changes'}
        </button>
        {adjustment !== null && (
          <button type="button" onClick={onCancel} disabled={save.isPending}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
