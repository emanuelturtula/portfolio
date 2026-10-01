import {
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  HELD_EXCEEDS_HISTORY_BADGE,
  HELD_EXCEEDS_HISTORY_EXPLANATION,
  type PositionFlag,
} from '@/lib/accounting';
import { Badge } from '@/pages/dashboard/Badge';

interface FlagLegendProps {
  /** Every flag shown anywhere in the section, each once - see `flagsOf`. */
  readonly flags: readonly PositionFlag[];
  /**
   * Whether any position - a row of the table, or an asset in the line for those no longer
   * held - carries the "Held exceeds history" marker. It is not a flag of the position, it
   * comes from the holdings check, but a marker the legend does not explain is a warning that
   * does not warn, so it is explained here with the others.
   */
  readonly heldExceedsHistory: boolean;
}

/**
 * What each data-quality marker means, for every marker on screen: on a row of the table, or
 * beside an asset in the line for those no longer held. A marker the legend does not explain
 * is a warning that does not warn, and the flags that matter most - `history_incomplete` and
 * `unattributed_fee` are sticky - often sit on an asset that has no row.
 */
export function FlagLegend({ flags, heldExceedsHistory }: FlagLegendProps) {
  if (flags.length === 0 && !heldExceedsHistory) {
    return null;
  }

  return (
    <dl className="flag-legend">
      {flags.map((flag) => (
        <div key={flag}>
          <dt>
            <Badge>{FLAG_BADGES[flag]}</Badge>
          </dt>
          <dd>{FLAG_EXPLANATIONS[flag]}</dd>
        </div>
      ))}
      {heldExceedsHistory && (
        <div>
          <dt>
            <Badge>{HELD_EXCEEDS_HISTORY_BADGE}</Badge>
          </dt>
          <dd>{HELD_EXCEEDS_HISTORY_EXPLANATION}</dd>
        </div>
      )}
    </dl>
  );
}
