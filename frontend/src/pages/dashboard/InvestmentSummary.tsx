import { Link } from 'react-router-dom';

import { describeApiError } from '@/api/client';
import { useInvestment, type AssetInvestment, type TotalInvestment } from '@/api/operations';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import {
  describeDifference,
  describeProfit,
  formatQuantity,
  formatSignedPercent,
  formatUsdt,
  investmentUnavailableWords,
} from '@/lib/investment';
import { money } from '@/lib/money';

export const INVESTMENT_LOADING_LABEL = 'Loading what was invested…';

const LOAD_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const REFETCH_FAILURE_FALLBACK = 'The server could not be reached.';

/** An amount, or a dash when it is unknown: never a zero standing in for one. */
function usdtOrDash(amount: string | null): string {
  return amount === null ? '—' : formatUsdt(money(amount));
}

/** The profit and its percentage, or the sentence saying why there is none. */
function ProfitText({ figures }: { readonly figures: TotalInvestment | AssetInvestment }) {
  if (figures.pnl === null) {
    return (
      <span className="unavailable">
        {investmentUnavailableWords(figures.unavailable ?? 'value_unknown')}
      </span>
    );
  }
  const profit = describeProfit(money(figures.pnl));
  return (
    <span className={`change-${profit.direction}`}>
      {profit.text}
      {figures.pnl_pct === null
        ? ` (${investmentUnavailableWords(figures.unavailable ?? 'nothing_invested')})`
        : ` (${formatSignedPercent(money(figures.pnl_pct))})`}
    </span>
  );
}

function AssetRow({ asset }: { readonly asset: AssetInvestment }) {
  return (
    <tr>
      <th scope="row">{asset.asset}</th>
      <td className="num">{usdtOrDash(asset.invested)}</td>
      <td className="num">{usdtOrDash(asset.value)}</td>
      <td>
        <ProfitText figures={asset} />
      </td>
      <td className="num">
        {asset.held === null ? 'Not read yet' : formatQuantity(money(asset.held), asset.asset)}
      </td>
      <td className="num">{formatQuantity(money(asset.explained), asset.asset)}</td>
      <td>
        {asset.difference === null
          ? 'Unknown until the wallet is read.'
          : describeDifference(money(asset.difference), asset.asset)}
      </td>
    </tr>
  );
}

/**
 * The dashboard's Invested section (spec 042): what went in, in USDT, what it is worth now,
 * and the gain or loss, each with a sign and a word as well as a colour. Per asset, the
 * quantity the wallets hold beside the quantity the uploaded operations explain.
 *
 * Its own query and its own four states, like the change widget. With no operation uploaded
 * yet, it says so and links to the page that takes the files.
 */
export function InvestmentSummary() {
  const investment = useInvestment();

  return (
    <section className="card investment-card" aria-labelledby="investment-heading">
      <div className="card-head">
        <div className="history-title">
          <h2 id="investment-heading">Invested</h2>
          <span className="page-meta">USDT</span>
        </div>
        <Link to="/operations">Operations</Link>
      </div>
      {investment.isPending ? (
        <Skeleton label={INVESTMENT_LOADING_LABEL} />
      ) : investment.isLoadingError ? (
        <ErrorState
          title="Could not load what was invested"
          headingLevel={3}
          description={describeApiError(investment.error, LOAD_FAILURE_FALLBACK)}
          onRetry={() => {
            void investment.refetch();
          }}
        />
      ) : investment.data.assets.every((asset) => asset.trades === 0) ? (
        <p className="history-empty">
          No operations yet. <Link to="/operations">Upload your exchange reports</Link> to see what
          was invested and the gain or loss.
        </p>
      ) : (
        <>
          {investment.isRefetchError && (
            <p className="note note-error" role="alert">
              Could not refresh what was invested:{' '}
              {describeApiError(investment.error, REFETCH_FAILURE_FALLBACK)} Showing what was last
              loaded.
            </p>
          )}
          <dl className="investment-totals">
            <div>
              <dt className="kpi-label">Invested</dt>
              <dd className="change-value">{usdtOrDash(investment.data.overall.invested)}</dd>
            </div>
            <div>
              <dt className="kpi-label">Worth now</dt>
              <dd className="change-value">{usdtOrDash(investment.data.overall.value)}</dd>
            </div>
            <div>
              <dt className="kpi-label">Gain or loss</dt>
              <dd className="change-value">
                <ProfitText figures={investment.data.overall} />
              </dd>
            </div>
          </dl>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="visually-hidden">
                Invested, value and gain or loss per asset, and the quantity the operations explain
              </caption>
              <thead>
                <tr>
                  <th scope="col">Asset</th>
                  <th scope="col" className="num">
                    Invested
                  </th>
                  <th scope="col" className="num">
                    Worth now
                  </th>
                  <th scope="col">Gain or loss</th>
                  <th scope="col" className="num">
                    In wallets
                  </th>
                  <th scope="col" className="num">
                    From operations
                  </th>
                  <th scope="col">Check</th>
                </tr>
              </thead>
              <tbody>
                {investment.data.assets.map((asset) => (
                  <AssetRow key={asset.asset} asset={asset} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
