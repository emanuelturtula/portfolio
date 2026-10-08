import { ValueSection } from '@/pages/dashboard/ValueSection';

/**
 * Everything behind the dashboard's figures: what each wallet holds, what it is worth and
 * the sync state. See docs/specs/011-wallets-page-value-dashboard.md.
 *
 * This was the dashboard until #154 gave the dashboard its figures, the holdings and their
 * distribution. It is reached from the header's Details link, for when the summary's figures
 * need explaining.
 */
export function DetailsPage() {
  return <ValueSection />;
}
