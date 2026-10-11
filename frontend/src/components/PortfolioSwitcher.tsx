import { usePortfolioScope } from '@/lib/portfolioScope';

const ALL = 'all';

/**
 * Chooses the portfolio every page shows, or all of them together (spec 044, R11). Shown only
 * with two or more portfolios: with one, *All* is that portfolio and there is nothing to choose.
 */
export function PortfolioSwitcher() {
  const { scope, portfolios, choose } = usePortfolioScope();

  if (portfolios.length < 2) {
    return null;
  }

  return (
    <div className="field portfolio-switcher">
      <label htmlFor="portfolio-switcher">Portfolio</label>
      <select
        id="portfolio-switcher"
        value={scope === null ? ALL : String(scope)}
        onChange={(event) => {
          const found = portfolios.find((portfolio) => String(portfolio.id) === event.target.value);
          choose(found === undefined ? null : found.id);
        }}
      >
        <option value={ALL}>All portfolios</option>
        {portfolios.map((portfolio) => (
          <option key={portfolio.id} value={String(portfolio.id)}>
            {portfolio.name}
          </option>
        ))}
      </select>
    </div>
  );
}
