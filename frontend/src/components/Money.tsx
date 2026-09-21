import { formatMoney, type FormatMoneyOptions, type Money as MoneyValue } from '@/lib/money';

interface MoneyProps {
  readonly value: MoneyValue;
  readonly options?: FormatMoneyOptions;
}

/**
 * Renders a monetary amount into a `<data>` element whose `value` attribute
 * carries the exact, unformatted decimal string. The visible text may be
 * grouped and rounded for readability; the attribute is what proves no
 * precision was lost, because it is the one thing here that is never rounded.
 */
export function Money({ value, options }: MoneyProps) {
  return <data value={value}>{formatMoney(value, options)}</data>;
}
