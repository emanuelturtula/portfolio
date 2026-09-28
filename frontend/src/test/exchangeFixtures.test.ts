import { describe, expect, it } from 'vitest';

import {
  ALL_EXCHANGE_KEYS,
  authFailedExchange,
  drainedBeforeMarkSynced,
  erroredExchange,
} from './exchangeFixtures';

/**
 * The control on the fixture guard's plan rule, for both venues.
 *
 * Spec 017 settles what #16 left open: BingX, like Bitget, needs no symbol, so
 * its sync commits the plan before the first fetch, and a failure that came
 * out of a fetch always has `requested_since` set and a window still queued.
 * These tests show the guard now refuses the impossible BingX state, and
 * accepts the writable one beside it, so its silence elsewhere means something.
 */
describe('the exchange fixture guard', () => {
  it.each(ALL_EXCHANGE_KEYS)(
    'refuses a %s auth failure from a fetch with no plan behind it',
    (key) => {
      expect(() =>
        authFailedExchange('auth', {
          exchange_key: key,
          requested_since: null,
          effective_since: null,
          pending_windows: 0,
        }),
      ).toThrow(`${key}: auth_failed came out of a fetch, and the plan is committed before any.`);
    },
  );

  it.each(ALL_EXCHANGE_KEYS)(
    'refuses a %s error from a fetch with no window left queued',
    (key) => {
      expect(() =>
        erroredExchange('unavailable', { exchange_key: key, pending_windows: 0 }),
      ).toThrow(`${key}: the window a failed fetch was reading is still queued.`);
    },
  );

  it.each(ALL_EXCHANGE_KEYS)('accepts the writable %s failures beside them', (key) => {
    const refused = authFailedExchange('auth', { exchange_key: key });
    const errored = erroredExchange('unavailable', { exchange_key: key });

    expect(refused.requested_since).not.toBeNull();
    expect(refused.pending_windows).toBeGreaterThan(0);
    expect(errored.pending_windows).toBeGreaterThan(0);
    expect(drainedBeforeMarkSynced(refused).pending_windows).toBe(0);
  });
});
