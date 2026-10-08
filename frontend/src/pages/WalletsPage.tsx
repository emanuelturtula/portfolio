import { ValueSection } from '@/pages/dashboard/ValueSection';
import { WalletForm } from '@/pages/wallets/WalletForm';
import { WalletList } from '@/pages/wallets/WalletList';

/**
 * Every wallet in one place (spec 039): what each holds and is worth, then the registry that
 * adds and archives them. See docs/specs/011-wallets-page-value-dashboard.md for both halves.
 *
 * The balances were the Details page until spec 039 folded it in here, unchanged: the readings
 * and sync state behind the dashboard's figures, and one wallet's value over time. `/details`
 * now redirects to this page.
 *
 * The balances, the form and the list are independent components on purpose. Registering an
 * address needs nothing either query provides, so a failed balances read or a failed list load
 * must not take the form down with it (spec 011: "the add form still works when the list fails
 * to load"; spec 039, R2).
 */
export function WalletsPage() {
  return (
    <div className="page">
      <ValueSection />
      <section className="page-section" aria-labelledby="manage-wallets-heading">
        <h2 id="manage-wallets-heading" className="section-title">
          Manage wallets
        </h2>
        <div className="side-layout">
          <WalletForm />
          <WalletList />
        </div>
      </section>
    </div>
  );
}
