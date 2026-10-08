/**
 * The words for the Health page's sections after Backups: the timers, the balance sync per
 * chain and the prices. See
 * docs/specs/030-observability.md, "Design: frontend".
 *
 * No React anywhere in this module - it is exercised directly by tests, the same split
 * `lib/backups.ts` uses for the same reason.
 *
 * Every `Record` below is keyed by a generated union, so a state added on the backend fails
 * `tsc` here until it has words. A state nobody can read the meaning of is a status line
 * that says nothing. Each sentence opens with a one-word label, so a state reads without
 * relying on colour or position.
 *
 * `unavailable` is not in any table. It is a section that could not be built at all, it is
 * one sentence ({@link UNAVAILABLE_WORDS}) for every section, and the tables are keyed by
 * `Exclude<State, 'unavailable'>` so the type still forces every other state to have words.
 */
import type { PriceHealthState, SchedulerName, SchedulerState, SourceState } from '@/api/health';

/**
 * What a section says when the backend could not build it, whatever the section. The cause
 * is in the container log, and no reason is served: the section's own data is the thing that
 * could not be read.
 */
export const UNAVAILABLE_WORDS = 'Could not be read. The log says why.';

/** Where a date would be and there is none, for a source that has not done the thing yet. */
export const NEVER_WORDS = 'never';

/** The last tick of a timer, before any: the record is in memory, so a restart clears it. */
export const NO_TICK_WORDS = 'none since the server started';

/** The timers, by the names the backend gives them. */
export const TIMER_NAMES: Record<SchedulerName, string> = {
  'balance-sync': 'Balance sync',
  'price-refresh': 'Price refresh',
  backup: 'Backup',
};

/**
 * A timer's state in words. `late` is a timer that is running but has not finished a tick
 * within twice its interval; it is not `stopped`, and it does not say the work is failing.
 */
export const TIMER_STATE_WORDS: Record<SchedulerState, string> = {
  ok: 'OK. The timer is running on schedule.',
  late: 'Late. The timer is running but has not finished a tick when it should have.',
  stopped: 'Stopped. The timer is not running.',
  disabled: 'Disabled. This timer is switched off on this server.',
};

/** How the last finished tick of a timer ended. */
export const TICK_SUCCEEDED_WORDS = 'Succeeded.';
export const TICK_FAILED_WORDS = 'Failed. The log says why.';

/**
 * What a list says when it has nothing to list. "Nothing yet" is a sync that worked and found
 * nothing, which is the opposite of a section that could not be read ({@link UNAVAILABLE_WORDS}),
 * and the two are never worded alike.
 */
export interface EmptyWords {
  readonly title: string;
  readonly description: string;
}

/** The balance sync, one chain at a time. */
export const CHAINS_EMPTY_WORDS: EmptyWords = {
  title: 'No chains to report yet',
  description: 'Add a wallet and the balance sync will report on its chain here.',
};

export const CHAIN_STATE_WORDS: Record<SourceState, string> = {
  ok: 'OK. The last balance sync read this chain.',
  failing: 'Failing. The last balance sync could not read this chain.',
  never: 'Never read. No finished balance sync has an outcome for this chain yet.',
};

/**
 * The prices' freshness. `stale` is the one that matters: every value that uses a price is
 * worth less than it looks, and the page says so rather than showing the old price alone.
 */
export const PRICES_STATE_WORDS: Record<Exclude<PriceHealthState, 'unavailable'>, string> = {
  fresh: 'Fresh. Prices were fetched recently.',
  stale:
    'Stale. Prices have not been fetched recently, so values that use them may be out of date.',
  never: 'Never fetched. No price has been fetched yet.',
};
