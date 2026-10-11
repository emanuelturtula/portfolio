import { useState, type SubmitEvent } from 'react';

import { describeApiError } from '@/api/client';
import {
  useCreatePortfolio,
  useDeletePortfolio,
  usePortfolios,
  useRenamePortfolio,
  type Portfolio,
} from '@/api/portfolios';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';

export const PORTFOLIOS_LOADING_LABEL = 'Loading your portfolios…';

const LOAD_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const WRITE_FAILURE_FALLBACK = 'The server could not be reached.';

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

/** What the portfolio holds, in words. Counts, not money. */
export function describeContents(portfolio: Portfolio): string {
  return `${plural(portfolio.wallets, 'wallet', 'wallets')}, ${plural(
    portfolio.operations,
    'operation',
    'operations',
  )}`;
}

/** Why a portfolio cannot be deleted, or `undefined` when it can (spec 044, R10). */
export function deleteBlocker(portfolio: Portfolio, count: number): string | undefined {
  if (count === 1) {
    return 'The only portfolio cannot be deleted.';
  }
  if (portfolio.wallets > 0 || portfolio.operations > 0) {
    return 'Move its wallets and operations to another portfolio to delete it.';
  }
  return undefined;
}

function CreateForm() {
  const create = useCreatePortfolio();
  const [name, setName] = useState('');

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(name.trim(), {
      onSuccess: () => {
        setName('');
      },
    });
  }

  return (
    <form className="card" onSubmit={handleSubmit} noValidate aria-labelledby="new-portfolio">
      <h3 id="new-portfolio">New portfolio</h3>
      <p className="hint">
        For money kept for a purpose of its own, such as retirement or trading. Add wallets and
        operations to it, or move them there from another portfolio.
      </p>
      <div className="field">
        <label htmlFor="portfolio-name">Name</label>
        <input
          id="portfolio-name"
          type="text"
          value={name}
          maxLength={60}
          onChange={(event) => {
            setName(event.target.value);
            create.reset();
          }}
        />
      </div>
      <button
        type="submit"
        className="button-primary"
        disabled={name.trim() === '' || create.isPending}
      >
        Create
      </button>
      {create.isError && (
        <p className="note note-error" role="alert">
          Not created: {describeApiError(create.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
      {create.isSuccess && (
        <p className="note" role="status">
          Created {create.data.name}.
        </p>
      )}
    </form>
  );
}

interface PortfolioRowProps {
  readonly portfolio: Portfolio;
  readonly count: number;
}

function PortfolioRow({ portfolio, count }: PortfolioRowProps) {
  const rename = useRenamePortfolio();
  const remove = useDeletePortfolio();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(portfolio.name);
  const blocker = deleteBlocker(portfolio, count);
  const inputId = `portfolio-rename-${String(portfolio.id)}`;

  return (
    <li className="wallet-row">
      {editing ? (
        <form
          className="portfolio-rename"
          onSubmit={(event) => {
            event.preventDefault();
            rename.mutate(
              { id: portfolio.id, name: name.trim() },
              {
                onSuccess: () => {
                  setEditing(false);
                },
              },
            );
          }}
        >
          <div className="field">
            <label htmlFor={inputId}>New name for {portfolio.name}</label>
            <input
              id={inputId}
              type="text"
              value={name}
              maxLength={60}
              onChange={(event) => {
                setName(event.target.value);
                rename.reset();
              }}
            />
          </div>
          <div className="wallet-row-actions">
            <button type="submit" disabled={name.trim() === '' || rename.isPending}>
              Save
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setName(portfolio.name);
                rename.reset();
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="wallet-row-main">
            <span className="wallet-chain">{portfolio.name}</span>
            <span className="wallet-label">{describeContents(portfolio)}</span>
          </div>
          <div className="wallet-row-actions">
            <button
              type="button"
              aria-label={`Rename ${portfolio.name}`}
              onClick={() => {
                setEditing(true);
              }}
            >
              Rename
            </button>
            <button
              type="button"
              aria-label={`Delete ${portfolio.name}`}
              disabled={blocker !== undefined || remove.isPending}
              onClick={() => {
                remove.mutate(portfolio.id);
              }}
            >
              Delete
            </button>
          </div>
          {blocker !== undefined && <p className="hint">{blocker}</p>}
        </>
      )}
      {rename.isError && (
        <p role="alert">Not renamed: {describeApiError(rename.error, WRITE_FAILURE_FALLBACK)}</p>
      )}
      {remove.isError && (
        <p role="alert">Not deleted: {describeApiError(remove.error, WRITE_FAILURE_FALLBACK)}</p>
      )}
    </li>
  );
}

/** Every portfolio, with what it holds; four states. */
function PortfolioList() {
  const portfolios = usePortfolios();

  if (portfolios.isPending) {
    return <Skeleton label={PORTFOLIOS_LOADING_LABEL} />;
  }

  if (portfolios.isError) {
    return (
      <ErrorState
        title="Could not load your portfolios"
        headingLevel={3}
        description={describeApiError(portfolios.error, LOAD_FAILURE_FALLBACK)}
        onRetry={() => {
          void portfolios.refetch();
        }}
      />
    );
  }

  if (portfolios.data.length === 0) {
    return (
      <EmptyState
        title="No portfolios yet"
        description="The first one, Main, is made when you add a wallet or upload a report. You can also create one here."
        headingLevel={3}
      />
    );
  }

  return (
    <section className="card" aria-labelledby="portfolio-list-heading">
      <h3 id="portfolio-list-heading">Your portfolios</h3>
      <ul className="wallet-list">
        {portfolios.data.map((portfolio) => (
          <PortfolioRow key={portfolio.id} portfolio={portfolio} count={portfolios.data.length} />
        ))}
      </ul>
    </section>
  );
}

/**
 * The Portfolios page (spec 044): create, rename and delete portfolios. Each portfolio's
 * wallets and operations are on the other pages, with the portfolio chosen at the top of
 * every page once there are two or more.
 */
export function PortfoliosPage() {
  return (
    <div className="page">
      <div className="page-head">
        <h2 className="page-title">Portfolios</h2>
      </div>
      <div className="side-layout">
        <CreateForm />
        <PortfolioList />
      </div>
    </div>
  );
}
