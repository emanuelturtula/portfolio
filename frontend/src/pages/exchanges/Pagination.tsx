import { describePage, describeShowing, pageCount } from '@/lib/fills';

interface PaginationProps {
  /** 1-based, and never past the last page: the caller handles a page that is. */
  readonly page: number;
  readonly total: number;
  readonly onPageChange: (page: number) => void;
}

/**
 * "Showing 51 to 100 of 812", the page number, and Previous / Next.
 *
 * At either end the button is `aria-disabled` and its click does nothing, not `disabled`: a
 * natively disabled button drops the keyboard's focus to `<body>` the moment it takes effect,
 * so the owner paging with Enter would lose their place on the last page (spec 016, R10).
 */
export function Pagination({ page, total, onPageChange }: PaginationProps) {
  const atStart = page === 1;
  const atEnd = page === pageCount(total);

  return (
    <nav className="pagination" aria-label="Pagination">
      <p>{describeShowing(page, total)}</p>
      <p>{describePage(page, total)}</p>
      <div className="pagination-buttons">
        <button
          type="button"
          aria-disabled={atStart ? 'true' : undefined}
          onClick={() => {
            if (!atStart) {
              onPageChange(page - 1);
            }
          }}
        >
          Previous
        </button>
        <button
          type="button"
          aria-disabled={atEnd ? 'true' : undefined}
          onClick={() => {
            if (!atEnd) {
              onPageChange(page + 1);
            }
          }}
        >
          Next
        </button>
      </div>
    </nav>
  );
}
