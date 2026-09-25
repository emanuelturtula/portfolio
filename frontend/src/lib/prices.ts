/**
 * The sentence shown for each reason an asset could not be priced.
 *
 * A `Record` keyed by the generated `PriceUnavailable` union, so that a reason added on the
 * backend fails `tsc` here until it has a sentence - the same treatment
 * `lib/freshness.ts` gives `SyncErrorKind`.
 */
import type { components } from '@/api/generated/schema';

export type PriceUnavailable = components['schemas']['PriceUnavailable'];

export const PRICE_UNAVAILABLE_MESSAGES: Record<PriceUnavailable, string> = {
  never_fetched: 'Prices have not been fetched yet.',
  every_source_failed: 'Every price source failed.',
  unsupported_pair: 'This asset is not priced in this currency.',
  no_source_configured: 'No price source is configured for this asset.',
};
