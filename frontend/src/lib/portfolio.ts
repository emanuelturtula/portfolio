/**
 * What the dashboard says about a summary that is not whole (#154): which figure each gap
 * touches, and a few words for it.
 */
import type { MissingKind, MissingPiece } from '@/api/portfolio';
import { chainDisplayName } from '@/lib/chains';
import { EXCHANGES, type ExchangeKey } from '@/lib/exchanges';

/** The kinds that leave the total value short or out of date, and so the P/L with it. */
const VALUE_KINDS: ReadonlySet<MissingKind> = new Set([
  'wallet_unread',
  'wallet_stale',
  'exchange_unread',
  'exchange_stale',
  'unpriced',
  'stale_price',
]);

/** Whether any gap touches the total value (and so the P/L). */
export function valueIsPartial(missing: readonly MissingPiece[]): boolean {
  return missing.some((piece) => VALUE_KINDS.has(piece.kind));
}

/** Whether any gap touches the invested figure (and so the P/L). */
export function investedIsPartial(missing: readonly MissingPiece[]): boolean {
  return missing.some((piece) => piece.kind === 'fill_not_in_cash');
}

function venueName(key: string): string {
  return key in EXCHANGES ? EXCHANGES[key as ExchangeKey].name : key;
}

/** A few words for one gap, written to sit in a list after "Incomplete:". */
export function describeMissing(piece: MissingPiece): string {
  switch (piece.kind) {
    case 'wallet_unread':
      return `${chainDisplayName(piece.subject)} wallet not read yet`;
    case 'wallet_stale':
      return `${chainDisplayName(piece.subject)} balance out of date`;
    case 'exchange_unread':
      return `${venueName(piece.subject)} balances not read yet`;
    case 'exchange_stale':
      return `${venueName(piece.subject)} balances out of date`;
    case 'unpriced':
      return `no price for ${piece.subject}`;
    case 'stale_price':
      return `${piece.subject} price out of date`;
    case 'fill_not_in_cash':
      return `trades paid in ${piece.subject} not counted`;
  }
}
