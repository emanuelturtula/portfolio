import { describe, expect, it } from 'vitest';

import {
  assessFreshness,
  freshnessMessage,
  FRESHNESS_MESSAGES,
  INTERRUPTED_MESSAGE,
  NEVER_SYNCED_MESSAGE,
  NOT_COVERED_MESSAGE,
  selectSettledRun,
  SYNC_ERROR_MESSAGES,
  UNKNOWN_FAILURE_MESSAGE,
} from '@/lib/freshness';
import {
  chainOutcome,
  failedOutcome,
  interruptedRun,
  previousRun,
  PREVIOUS_OBSERVED_AT,
  RUN_STARTED_AT,
  runningRun,
  syncRun,
  type SyncErrorKind,
} from '@/test/fixtures';

/**
 * Every `SyncErrorKind` the generated schema declares, written out by hand.
 *
 * Typed as a `Record` whose values are ignored so that adding a member to the
 * union fails `tsc` right here until this list names it - the test cannot
 * quietly cover five kinds out of six.
 */
const ALL_ERROR_KINDS_RECORD: Record<SyncErrorKind, true> = {
  unavailable: true,
  rate_limited: true,
  response: true,
  unknown_chain: true,
  address_rejected: true,
  internal: true,
};
const ALL_ERROR_KINDS = Object.keys(ALL_ERROR_KINDS_RECORD) as SyncErrorKind[];

/** One millisecond before the latest run started. */
const JUST_BEFORE_RUN = '2026-09-24T11:39:59.999Z';

describe('selectSettledRun', () => {
  it('has nothing to judge against when no run has ever happened', () => {
    expect(selectSettledRun([])).toEqual({ settled: undefined, inProgress: false });
  });

  it('takes the newest run when it has finished', () => {
    const latest = syncRun();
    const older = previousRun();

    expect(selectSettledRun([latest, older])).toEqual({ settled: latest, inProgress: false });
  });

  it('falls through a running first run to the second, and says a sync is running', () => {
    // The run in flight has no outcomes yet, so judging against it would call
    // every wallet "not covered" for the whole duration of every sync.
    const running = runningRun();
    const latest = syncRun();

    const selected = selectSettledRun([running, latest]);

    expect(selected.settled).toBe(latest);
    expect(selected.inProgress).toBe(true);
  });

  it('has nothing settled when the first run ever is still running', () => {
    expect(selectSettledRun([runningRun()])).toEqual({ settled: undefined, inProgress: true });
  });

  it.each(['success', 'partial', 'failed', 'interrupted'] as const)(
    'treats a %s run as settled',
    (status) => {
      const run = syncRun({ status });

      expect(selectSettledRun([run, previousRun()])).toEqual({ settled: run, inProgress: false });
    },
  );

  it('keeps an interrupted run as the settled one rather than skipping past it', () => {
    // The rule is "not running", not "finished cleanly". Skipping an
    // interrupted run would judge rows against an older run and call a reading
    // fresh that the interrupted run had every chance to refresh.
    const interrupted = interruptedRun();

    expect(selectSettledRun([interrupted, previousRun()]).settled).toBe(interrupted);
  });
});

describe('assessFreshness', () => {
  it('is fresh when the chain succeeded and the reading is from that run', () => {
    const settled = syncRun();

    expect(assessFreshness(settled, 'bitcoin', '2026-09-24T11:41:00.000Z')).toEqual({
      status: 'fresh',
    });
  });

  it('counts a reading stamped at the very instant the run started as fresh', () => {
    // "At or after", not "after". `observed_at` is stamped when a chain's read
    // begins, and a chain read first in the run can share the millisecond.
    expect(assessFreshness(syncRun(), 'bitcoin', RUN_STARTED_AT)).toEqual({ status: 'fresh' });
  });

  it('compares instants, not strings', () => {
    // Half a second into the run. As strings, "…11:40:00.500Z" sorts *before*
    // "…11:40:00Z" because "." < "Z", so a string comparison would call this
    // fresh reading stale.
    const settled = syncRun({ started_at: '2026-09-24T11:40:00Z' });

    expect(assessFreshness(settled, 'bitcoin', '2026-09-24T11:40:00.500Z').status).toBe('fresh');
  });

  it('compares instants across different offsets', () => {
    // 10:41 at -01:00 is 11:41 UTC, a minute after the run started, and sorts
    // *before* "11:40" as text. 12:39 at +01:00 is 11:39 UTC, a minute before
    // the run, and sorts *after* it as text. A string comparison gets both
    // wrong, in opposite directions.
    const settled = syncRun({ started_at: '2026-09-24T11:40:00.000Z' });

    expect(assessFreshness(settled, 'bitcoin', '2026-09-24T10:41:00.000-01:00').status).toBe(
      'fresh',
    );
    expect(assessFreshness(settled, 'bitcoin', '2026-09-24T12:39:00.000+01:00').status).toBe(
      'not_covered',
    );
  });

  it('treats sub-millisecond digits as within the same millisecond, never as older', () => {
    // The backend's timestamps carry microseconds, and `Date` keeps
    // milliseconds. Truncating both sides the same way cannot reorder them.
    const settled = syncRun({ started_at: '2026-09-24T11:40:00.123456Z' });

    expect(assessFreshness(settled, 'bitcoin', '2026-09-24T11:40:00.123999Z').status).toBe('fresh');
  });

  it('says no sync has run when there is no settled run', () => {
    expect(assessFreshness(undefined, 'bitcoin', null)).toEqual({ status: 'never_synced' });
    // Even with a reading: without a run there is nothing to call it fresh by.
    expect(assessFreshness(undefined, 'bitcoin', RUN_STARTED_AT)).toEqual({
      status: 'never_synced',
    });
  });

  it.each(ALL_ERROR_KINDS)('reports a %s failure with its kind', (errorKind) => {
    const settled = syncRun({
      status: 'partial',
      chains: [chainOutcome({ chain_key: 'bitcoin' }), failedOutcome('kaspa', errorKind)],
    });

    expect(assessFreshness(settled, 'kaspa', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'failed',
      errorKind,
    });
  });

  it('reports the failure for a wallet the failed chain never read', () => {
    // Unread and failed: the row says "Not read yet" and why.
    const settled = syncRun({ chains: [failedOutcome('kaspa', 'rate_limited')] });

    expect(assessFreshness(settled, 'kaspa', null)).toEqual({
      status: 'failed',
      errorKind: 'rate_limited',
    });
  });

  it('reports a failure whose kind is missing as a failure, not as fresh', () => {
    const settled = syncRun({
      chains: [chainOutcome({ chain_key: 'kaspa', status: 'failed', error_kind: null })],
    });

    expect(assessFreshness(settled, 'kaspa', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'failed',
      errorKind: null,
    });
  });

  it('judges each chain by its own outcome', () => {
    // One chain down must not drag the other chain's rows with it.
    const settled = syncRun({
      status: 'partial',
      chains: [chainOutcome({ chain_key: 'bitcoin' }), failedOutcome('kaspa', 'unavailable')],
    });

    expect(assessFreshness(settled, 'bitcoin', RUN_STARTED_AT).status).toBe('fresh');
    expect(assessFreshness(settled, 'kaspa', RUN_STARTED_AT).status).toBe('failed');
  });

  it('says the last sync was interrupted before this chain when it has no outcome', () => {
    const settled = interruptedRun({ chains: [chainOutcome({ chain_key: 'bitcoin' })] });

    expect(assessFreshness(settled, 'kaspa', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'interrupted',
    });
    expect(assessFreshness(settled, 'kaspa', null)).toEqual({ status: 'interrupted' });
  });

  it('calls an old reading on a chain an interrupted run did read not covered, not interrupted', () => {
    // The run got to Bitcoin and read it successfully, then died. A Bitcoin
    // reading older than the run (a restored wallet) was skipped by it, not
    // cut off by the interruption, so "interrupted before it read this chain"
    // would be false.
    const settled = interruptedRun({ chains: [chainOutcome({ chain_key: 'bitcoin' })] });

    expect(assessFreshness(settled, 'bitcoin', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'not_covered',
    });
    expect(assessFreshness(settled, 'bitcoin', null)).toEqual({ status: 'not_covered' });
  });

  it('still calls a chain an interrupted run did read fresh', () => {
    // Interrupted after Bitcoin: Bitcoin's rows are as current as they get.
    const settled = interruptedRun({ chains: [chainOutcome({ chain_key: 'bitcoin' })] });

    expect(assessFreshness(settled, 'bitcoin', RUN_STARTED_AT).status).toBe('fresh');
  });

  it('calls a chain with no outcome in a finished run not covered', () => {
    // A chain added to the registry after the run, or one the run had no
    // wallets for at the time.
    const settled = syncRun({ chains: [chainOutcome({ chain_key: 'bitcoin' })] });

    expect(assessFreshness(settled, 'kaspa', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'not_covered',
    });
  });

  it('calls a restored wallet whose reading predates the run not covered, though its chain succeeded', () => {
    // The case the second half of the fresh test exists for. The wallet was
    // archived during the latest run, so the run skipped it while reading the
    // chain's other wallets successfully. Restoring it brought back the old
    // reading. A chain-only rule would call that reading fresh.
    const settled = syncRun({ chains: [chainOutcome({ chain_key: 'bitcoin', wallets_read: 1 })] });

    expect(assessFreshness(settled, 'bitcoin', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'not_covered',
    });
  });

  it('calls a reading one millisecond before the run not covered', () => {
    expect(assessFreshness(syncRun(), 'bitcoin', JUST_BEFORE_RUN).status).toBe('not_covered');
  });

  it('calls an unread wallet on a chain that succeeded not covered, never fresh', () => {
    // Registered after the run began: the chain succeeded without it.
    expect(assessFreshness(syncRun(), 'bitcoin', null)).toEqual({ status: 'not_covered' });
  });

  it('never calls a chain fresh on an outcome that is neither success nor failed', () => {
    // The backend only writes `success` or `failed` per chain today, but the
    // schema types the field as the whole run-status union. Anything else must
    // fall through to "not covered" rather than being mistaken for a success.
    for (const status of ['running', 'partial', 'interrupted'] as const) {
      const settled = syncRun({ chains: [chainOutcome({ chain_key: 'bitcoin', status })] });

      expect(assessFreshness(settled, 'bitcoin', RUN_STARTED_AT).status).toBe('not_covered');
    }
  });

  it('puts a failed outcome ahead of an interrupted run', () => {
    // Order matters: the chain's own failure is the more specific reason.
    const settled = interruptedRun({ chains: [failedOutcome('kaspa', 'response')] });

    expect(assessFreshness(settled, 'kaspa', PREVIOUS_OBSERVED_AT)).toEqual({
      status: 'failed',
      errorKind: 'response',
    });
  });

  it('matches a chain by its exact key', () => {
    // An unknown chain this build has no name for is still judged by the run
    // log, and a near-miss key is not the same chain.
    const settled = syncRun({ chains: [chainOutcome({ chain_key: 'litecoin' })] });

    expect(assessFreshness(settled, 'litecoin', RUN_STARTED_AT).status).toBe('fresh');
    expect(assessFreshness(settled, 'Litecoin', RUN_STARTED_AT).status).toBe('not_covered');
  });
});

describe('freshnessMessage', () => {
  it('says nothing about a fresh row', () => {
    expect(freshnessMessage({ status: 'fresh' })).toBe('');
  });

  it.each(ALL_ERROR_KINDS)('has its own sentence for a %s failure', (errorKind) => {
    const message = freshnessMessage({ status: 'failed', errorKind });

    expect(message).toBe(SYNC_ERROR_MESSAGES[errorKind]);
    expect(message.trim().length).toBeGreaterThan(0);
  });

  it('gives every error kind a different sentence', () => {
    // Three different parties are to blame across these six kinds - the
    // vendor, the owner, and this application. One shared sentence would send
    // the owner to look in the wrong place.
    const sentences = ALL_ERROR_KINDS.map((errorKind) => SYNC_ERROR_MESSAGES[errorKind]);

    expect(new Set(sentences).size).toBe(ALL_ERROR_KINDS.length);
    expect(Object.keys(SYNC_ERROR_MESSAGES).sort()).toEqual([...ALL_ERROR_KINDS].sort());
  });

  it('does not blame the provider for an address the owner registered', () => {
    // `address_rejected` is the owner's configuration and `internal` is ours;
    // neither is the vendor being down.
    expect(SYNC_ERROR_MESSAGES.address_rejected).not.toMatch(/could not be reached|provider/i);
    expect(SYNC_ERROR_MESSAGES.internal).not.toMatch(/provider/i);
  });

  it("describes a refused address as the whole chain's problem", () => {
    // One refused address aborts the chain's read (until #54), so the sentence
    // is shown on every wallet of that chain. Worded about "this address", it
    // would accuse each of them in turn.
    expect(SYNC_ERROR_MESSAGES.address_rejected).toMatch(/chain/i);
    expect(SYNC_ERROR_MESSAGES.address_rejected).toMatch(/none of its wallets/i);
    expect(SYNC_ERROR_MESSAGES.address_rejected).not.toMatch(/this address/i);
  });

  it('sends an internal failure to the server log', () => {
    expect(SYNC_ERROR_MESSAGES.internal).toMatch(/server log/i);
  });

  it('still says the read failed when the kind is missing', () => {
    expect(freshnessMessage({ status: 'failed', errorKind: null })).toBe(UNKNOWN_FAILURE_MESSAGE);
    expect(freshnessMessage({ status: 'failed' })).toBe(UNKNOWN_FAILURE_MESSAGE);
    expect(UNKNOWN_FAILURE_MESSAGE.trim().length).toBeGreaterThan(0);
  });

  it.each([
    ['never_synced', NEVER_SYNCED_MESSAGE, /no sync has run yet/i],
    ['interrupted', INTERRUPTED_MESSAGE, /interrupted/i],
    ['not_covered', NOT_COVERED_MESSAGE, /not covered by the last sync/i],
  ] as const)('explains %s', (status, constant, pattern) => {
    expect(freshnessMessage({ status })).toBe(constant);
    expect(FRESHNESS_MESSAGES[status]).toBe(constant);
    expect(constant).toMatch(pattern);
  });

  it('gives each non-fresh status a different sentence', () => {
    const sentences = [
      NEVER_SYNCED_MESSAGE,
      INTERRUPTED_MESSAGE,
      NOT_COVERED_MESSAGE,
      UNKNOWN_FAILURE_MESSAGE,
      ...Object.values(SYNC_ERROR_MESSAGES),
    ];

    expect(new Set(sentences).size).toBe(sentences.length);
  });
});
