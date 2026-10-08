/**
 * What the dashboard says about a summary that is not whole (#154): whether the total value is
 * touched by a gap, and a few words for each gap.
 */
import type { MissingPiece } from '@/api/portfolio';
import { chainDisplayName } from '@/lib/chains';

/**
 * Whether any gap touches the total value. Every kind does: an unread wallet or an unpriced
 * asset adds nothing to it, and a stale reading or a stale price adds a figure that is no
 * longer current.
 */
export function valueIsPartial(missing: readonly MissingPiece[]): boolean {
  return missing.length > 0;
}

/** A few words for one gap, written to sit in a list after "Incomplete:". */
export function describeMissing(piece: MissingPiece): string {
  switch (piece.kind) {
    case 'wallet_unread':
      return `${chainDisplayName(piece.subject)} wallet not read yet`;
    case 'wallet_stale':
      return `${chainDisplayName(piece.subject)} balance out of date`;
    case 'unpriced':
      return `no price for ${piece.subject}`;
    case 'stale_price':
      return `${piece.subject} price out of date`;
  }
}
