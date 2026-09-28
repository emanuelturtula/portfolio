import { onTestFinished } from 'vitest';

/**
 * Moves the process's time zone for the rest of the current test, and puts it
 * back when the test finishes, pass or fail.
 *
 * Node re-reads `TZ` when it is assigned, so `Date`'s local getters and
 * `Intl` without an explicit `timeZone` follow at once. CI runs in UTC, where
 * a formatter that forgot its zone, or used it where it should not, passes by
 * accident; a test that means to catch that has to move the zone first.
 *
 * Deleting `TZ` does not move the zone back, so the machine's own zone is read
 * before the move and assigned again afterwards.
 */
export function inTimeZone(zone: string): void {
  const originalZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const originalTz = process.env.TZ;

  process.env.TZ = zone;

  onTestFinished(() => {
    process.env.TZ = originalZone;
    if (originalTz === undefined) {
      delete process.env.TZ;
    } else {
      process.env.TZ = originalTz;
    }
  });
}
