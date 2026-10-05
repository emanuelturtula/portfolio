import { WalletForm } from '@/pages/wallets/WalletForm';
import { WalletList } from '@/pages/wallets/WalletList';

/**
 * The wallet registry: list, add (with advisory chain hints), archive and restore. See
 * docs/specs/011-wallets-page-value-dashboard.md.
 *
 * The form and the list are independent components on purpose - registering an address
 * needs nothing the list query provides, so a failed list load must not take the form
 * down with it (spec: "the add form still works when the list fails to load").
 */
export function WalletsPage() {
  return (
    <section className="page" aria-labelledby="wallets-heading">
      <h2 id="wallets-heading" className="page-title">
        Wallets
      </h2>
      <div className="side-layout">
        <WalletForm />
        <WalletList />
      </div>
    </section>
  );
}
