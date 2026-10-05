import { FILLS_PAGE_SIZE } from '@/lib/fillFilters';
import { describePage, describeShowing, pageCount } from '@/lib/fills';

interface PaginationProps {
  /** 1-based, and never past the last page: the caller handles a page that is. */
  readonly page: number;
  readonly total: number;
  /** Rows to a page: the fills' own size unless the list pages by another. */
  readonly pageSize?: number;
  /** The navigation's name: two paged lists on one page need two. */
  readonly label?: string;
  readonly onPageChange: (page: number) => void;
}

/**
 * "Showing 6 to 10 of 812", the page number, and Previous / Next.
 *
 * At either end the button is `aria-disabled` and its click does nothing, not `disabled`: a
 * natively disabled button drops the keyboard's focus to `<body>` the moment it takes effect,
 * so the owner paging with Enter would lose their place on the last page (spec 016, R10).
 */
export function Pagination({
  page,
  total,
  pageSize = FILLS_PAGE_SIZE,
  label = 'Pagination',
  onPageChange,
}: PaginationProps) {
  const atStart = page === 1;
  const atEnd = page === pageCount(total, pageSize);

  return (
    <nav className="pagination" aria-label={label}>
      <p>{describeShowing(page, total, pageSize)}</p>
      <p>{describePage(page, total, pageSize)}</p>
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
