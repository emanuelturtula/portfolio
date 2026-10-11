/**
 * Which portfolio the app shows (spec 044, R11): one of them, or all of them together.
 *
 * The choice is the browser's, kept in `localStorage`, and *All portfolios* until changed. It
 * is read through {@link usePortfolioScope}, and every query that can be narrowed puts the
 * scope in its key and its URL with {@link withPortfolio}.
 *
 * **The scope is `null` (all) unless it names a portfolio that exists right now.** A
 * remembered portfolio that was deleted, or any choice while there is only one portfolio,
 * reads as all: with one portfolio the two are the same figures, and the selector is not
 * shown.
 *
 * Outside the provider - a page rendered on its own in a test - the scope is all and there
 * is nothing to choose from, which is what the app showed before portfolios existed.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

import { usePortfolios, type Portfolio } from '@/api/portfolios';
import { Skeleton } from '@/components/Skeleton';

export const SELECTED_PORTFOLIO_KEY = 'portfolio.selected';

export interface PortfolioScope {
  /** The portfolio every figure is narrowed to, or `null` for all of them. */
  readonly scope: number | null;
  /** Every portfolio, oldest first; empty until loaded or outside the provider. */
  readonly portfolios: readonly Portfolio[];
  readonly choose: (portfolio: number | null) => void;
  /**
   * A portfolio was chosen earlier and the list that says whether it still exists is being
   * read. Until it is, the scope cannot be known, and a page that read *all* in the
   * meantime would show the wrong figures without a selector to say whose they are.
   */
  readonly resolving: boolean;
}

const ALL: PortfolioScope = {
  scope: null,
  portfolios: [],
  choose: () => undefined,
  resolving: false,
};

const PortfolioScopeContext = createContext<PortfolioScope>(ALL);

/** The remembered id, as text: it is only ever compared with an id written out. */
function readStored(): string | null {
  try {
    return window.localStorage.getItem(SELECTED_PORTFOLIO_KEY);
  } catch {
    return null;
  }
}

function writeStored(portfolio: number | null): void {
  try {
    if (portfolio === null) {
      window.localStorage.removeItem(SELECTED_PORTFOLIO_KEY);
    } else {
      window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, String(portfolio));
    }
  } catch {
    // Storage blocked: the choice lasts until the page is reloaded.
  }
}

export function PortfolioScopeProvider({ children }: { readonly children: ReactNode }) {
  const portfolios = usePortfolios();
  const [chosen, setChosen] = useState<string | null>(readStored);

  const value = useMemo<PortfolioScope>(() => {
    const list = portfolios.data ?? [];
    const found = list.find((portfolio) => String(portfolio.id) === chosen);
    return {
      scope: list.length > 1 && found !== undefined ? found.id : null,
      portfolios: list,
      choose: (portfolio) => {
        setChosen(portfolio === null ? null : String(portfolio));
        writeStored(portfolio);
      },
      resolving: chosen !== null && portfolios.isLoading,
    };
  }, [portfolios.data, portfolios.isLoading, chosen]);

  return <PortfolioScopeContext value={value}>{children}</PortfolioScopeContext>;
}

export function usePortfolioScope(): PortfolioScope {
  return useContext(PortfolioScopeContext);
}

export const PORTFOLIO_SCOPE_LOADING_LABEL = 'Loading your portfolio…';

/** Holds the pages back while a remembered choice is {@link PortfolioScope.resolving}. */
export function PortfolioScopeGate({ children }: { readonly children: ReactNode }) {
  const { resolving } = usePortfolioScope();
  if (resolving) {
    return <Skeleton label={PORTFOLIO_SCOPE_LOADING_LABEL} />;
  }
  return children;
}

/** `path` narrowed to `scope`: unchanged for all portfolios. */
export function withPortfolio(path: string, scope: number | null): string {
  if (scope === null) {
    return path;
  }
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}portfolio=${String(scope)}`;
}

/** Where a new wallet or operation goes by default: the chosen portfolio, else the first. */
export function defaultTarget(scope: PortfolioScope): number | undefined {
  return scope.scope ?? scope.portfolios[0]?.id;
}
