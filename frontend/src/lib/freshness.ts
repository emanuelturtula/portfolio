/**
 * The freshness rule: whether one wallet row's reading is current, judged against the run
 * log rather than against an age threshold.
 *
 * **Age is the wrong test.** The frontend does not know the sync interval, and "older than
 * an hour" says nothing about whose fault that is. The run log records which chain failed
 * and why, so the rule reads that instead - see docs/specs/011-wallets-page-value-dashboard.md,
 * "Freshness comes from the run log, not from an age threshold", for the full account,
 * including why the second half of the fresh test (the timestamp comparison) is load-bearing
 * for a restored wallet.
 *
 * No React anywhere in this module: it is exercised directly by tests and by both pages.
 */
import type { components } from '@/api/generated/schema';

import { parseInstant } from '@/lib/time';

export type SyncRunSummary = components['schemas']['SyncRunResponse'];
export type SyncErrorKind = components['schemas']['SyncErrorKind'];

export interface SettledRun {
  /** The first run that is not `running`, or `undefined` when none exists in the window. */
  readonly settled: SyncRunSummary | undefined;
  /** Whether the newest run (necessarily not `settled` itself, in that case) is in flight. */
  readonly inProgress: boolean;
  /**
   * The in-flight run itself, when `inProgress` is true - a live run the coordinator is
   * still working through, or an orphan a crashed close-out left behind with nothing
   * actually running server-side. Both look identical from here: a `running` row with no
   * newer settled run in front of it. Either way, clicking refresh is the right move - it
   * joins a live run, or it is what sweeps the orphan - so the UI does not try to tell them
   * apart, only reports that a run started and has not finished.
   */
  readonly runningRun: SyncRunSummary | undefined;
}

/**
 * Picks the run every wallet row's freshness is judged against, out of
 * `GET /api/balances/runs?limit=2`, newest first.
 *
 * Two runs are enough because at most one run is ever `running`: the coordinator runs one
 * at a time and sweeps orphaned `running` rows before it opens its own. That invariant is
 * this function's whole reason for only looking at two runs instead of walking the log
 * until it finds a settled one.
 */
export function selectSettledRun(runs: readonly SyncRunSummary[]): SettledRun {
  const [first, second] = runs;

  if (first === undefined) {
    return { settled: undefined, inProgress: false, runningRun: undefined };
  }
  if (first.status !== 'running') {
    return { settled: first, inProgress: false, runningRun: undefined };
  }
  return { settled: second, inProgress: true, runningRun: first };
}

export type FreshnessStatus = 'fresh' | 'never_synced' | 'failed' | 'interrupted' | 'not_covered';

export interface Freshness {
  readonly status: FreshnessStatus;
  /** Only meaningful when `status` is `'failed'`. `null` or missing on the outcome itself. */
  readonly errorKind?: SyncErrorKind | null;
}

/**
 * Whether `observedAt` is at or after `settled.started_at` - the timestamp half of the
 * fresh test, shared by the ordinary path and the interrupted-run path below.
 *
 * `observed_at` is stamped when a chain's read begins inside a run, so it is never earlier
 * than that run's `started_at` - comparing at millisecond precision (via `parseInstant`,
 * not a string comparison that would assume a shared timestamp format or a shared number
 * of fractional digits) cannot make a fresh reading look older than it is.
 */
function readAtOrAfterStart(observedAt: string | null, settled: SyncRunSummary): boolean {
  return observedAt !== null && parseInstant(observedAt) >= parseInstant(settled.started_at);
}

/**
 * Judges one wallet row's reading against the settled run, in the order below. Verified
 * against the backend: `finish_run` writes a run's chain outcomes in the same transaction
 * as its final status, and the sweep that closes out an orphaned run only flips the status
 * column - so **an interrupted run always has `chains: []`**, even for a chain whose
 * snapshots it did commit before the process died, because a snapshot commits per chain,
 * well before the close-out that never ran. Step 4 exists because of that: a chain-outcome
 * lookup can never confirm an interrupted run actually read this chain, so the timestamp
 * comparison is the only evidence left, and it is reliable - see {@link readAtOrAfterStart}.
 *
 * 1. No settled run at all -> `never_synced`.
 * 2. This chain's outcome is `'success'` and the reading is at or after the run's start ->
 *    `fresh`.
 * 3. This chain's outcome is `'failed'` -> `failed`, with the reason.
 * 4. The settled run is `interrupted`, and the reading is at or after its start -> `fresh`.
 *    Read literally: this run reached this chain before it stopped.
 * 5. The settled run is `interrupted` otherwise -> `interrupted`.
 * 6. Anything else -> `not_covered`.
 */
export function assessFreshness(
  settled: SyncRunSummary | undefined,
  chainKey: string,
  observedAt: string | null,
): Freshness {
  if (settled === undefined) {
    return { status: 'never_synced' };
  }

  const outcome = settled.chains.find((chain) => chain.chain_key === chainKey);

  if (outcome?.status === 'success' && readAtOrAfterStart(observedAt, settled)) {
    return { status: 'fresh' };
  }

  if (outcome?.status === 'failed') {
    return { status: 'failed', errorKind: outcome.error_kind };
  }

  if (settled.status === 'interrupted') {
    return readAtOrAfterStart(observedAt, settled)
      ? { status: 'fresh' }
      : { status: 'interrupted' };
  }

  return { status: 'not_covered' };
}

export const NEVER_SYNCED_MESSAGE = 'No sync has finished yet.';
/**
 * Worded around "this wallet", not "this chain": an interrupted run can still have read
 * the chain - other wallets on it may show `fresh` - while this specific wallet's own
 * reading predates the run, because it was archived and skipped when the run reached that
 * chain and only came back on restore. "Before it read this chain" would be false right
 * next to a sibling row saying "Up to date"; "before it read this wallet" is true either
 * way, whether the chain was never reached at all or was reached without this wallet.
 */
export const INTERRUPTED_MESSAGE = 'The last sync was interrupted before it read this wallet.';
export const NOT_COVERED_MESSAGE = 'Not covered by the last sync.';
/** Shown for a `'failed'` status whose outcome carries no `error_kind` (should not happen). */
export const UNKNOWN_FAILURE_MESSAGE = 'The last sync could not read this wallet.';

/** One sentence per non-fresh status that does not need an `error_kind` to explain itself. */
export const FRESHNESS_MESSAGES: Record<Exclude<FreshnessStatus, 'fresh' | 'failed'>, string> = {
  never_synced: NEVER_SYNCED_MESSAGE,
  interrupted: INTERRUPTED_MESSAGE,
  not_covered: NOT_COVERED_MESSAGE,
};

/**
 * One sentence per `SyncErrorKind`, keyed by the generated union so that a chain provider
 * failure kind added on the backend fails `tsc` here until it has a sentence.
 */
export const SYNC_ERROR_MESSAGES: Record<SyncErrorKind, string> = {
  unavailable: 'The provider could not be reached.',
  rate_limited: 'The provider is rate-limiting requests.',
  response: 'The provider sent a response that could not be used.',
  unknown_chain: 'This chain is not configured on the server.',
  // Per-chain, not per-wallet: until #54 isolates a single address's refusal, one rejected
  // address aborts the whole chain's read, so this sentence must not read as if it blames
  // the specific wallet it happens to be shown next to - every other wallet on the chain
  // was refused for the same reason.
  address_rejected:
    'An address on this chain was refused, so none of its wallets were read. Check that every address belongs to the network this server reads.',
  internal: 'An internal error interrupted the read; see the server log.',
};

/** The sentence for a `Freshness` whose status is not `'fresh'`. Empty for `'fresh'` itself. */
export function freshnessMessage(freshness: Freshness): string {
  if (freshness.status === 'fresh') {
    return '';
  }

  if (freshness.status === 'failed') {
    return freshness.errorKind == null
      ? UNKNOWN_FAILURE_MESSAGE
      : SYNC_ERROR_MESSAGES[freshness.errorKind];
  }

  return FRESHNESS_MESSAGES[freshness.status];
}
