import { useState } from 'react';

import type { Portfolio } from '@/api/portfolios';
import { defaultTarget, usePortfolioScope } from '@/lib/portfolioScope';

/**
 * The portfolio a form writes to, and whether there is a choice to show (spec 044, R12): with
 * two or more portfolios there is, and a portfolio to show as picked.
 */
export type PortfolioTarget =
  | {
      readonly shown: true;
      /** The portfolio picked, or the default: the selected one, else the first. */
      readonly value: number;
      readonly portfolios: readonly Portfolio[];
      readonly pick: (portfolio: number) => void;
    }
  | { readonly shown: false };

/**
 * The portfolio a form writes to. It follows the selection at the top of the page until the
 * owner picks one here, and a pick that no longer exists falls back to it too.
 */
export function usePortfolioTarget(): PortfolioTarget {
  const scope = usePortfolioScope();
  const [picked, setPicked] = useState<number | undefined>(undefined);
  const valid = scope.portfolios.some((portfolio) => portfolio.id === picked);
  const value = valid ? picked : defaultTarget(scope);
  if (scope.portfolios.length > 1 && value !== undefined) {
    return { shown: true, value, portfolios: scope.portfolios, pick: setPicked };
  }
  return { shown: false };
}

/** `{ portfolio_id }` for a request body when there is a choice, and nothing otherwise. */
export function portfolioBody(target: PortfolioTarget): { portfolio_id?: number } {
  return target.shown ? { portfolio_id: target.value } : {};
}

interface PortfolioSelectProps {
  readonly id: string;
  readonly label: string;
  readonly portfolios: readonly Portfolio[];
  readonly value: number;
  readonly onChange: (portfolio: number) => void;
  readonly disabled?: boolean;
}

/** A select over the portfolios, by name. */
export function PortfolioSelect({
  id,
  label,
  portfolios,
  value,
  onChange,
  disabled,
}: PortfolioSelectProps) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={String(value)}
        disabled={disabled}
        onChange={(event) => {
          const found = portfolios.find((portfolio) => String(portfolio.id) === event.target.value);
          if (found !== undefined) {
            onChange(found.id);
          }
        }}
      >
        {portfolios.map((portfolio) => (
          <option key={portfolio.id} value={String(portfolio.id)}>
            {portfolio.name}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The *Portfolio* field of a form, shown only when there is a choice (spec 044, R12). */
export function PortfolioField({
  id,
  target,
}: {
  readonly id: string;
  readonly target: PortfolioTarget;
}) {
  if (!target.shown) {
    return null;
  }
  return (
    <PortfolioSelect
      id={id}
      label="Portfolio"
      portfolios={target.portfolios}
      value={target.value}
      onChange={target.pick}
    />
  );
}
