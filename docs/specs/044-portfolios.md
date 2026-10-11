# 044 — Portfolios: several portfolios, each with its own wallets and operations

Issue: none; the owner's request of 2026-10-11
Status: in progress

## Problem

The app has one portfolio: every wallet and every exchange operation adds up into one total,
one chart, one invested figure. The owner keeps money for different purposes (a retirement
pot, a trading pot) and wants to see each one on its own: its value, its change, its history,
what went into it and whether its operations explain its wallets.

## Scope / Non-goals

- **`portfolios`**: a named group, owned by the account. Every wallet and every exchange
  operation belongs to exactly one.
- **The existing data becomes the first portfolio**, named `Main`, which the owner can rename.
- **Every figure can be read for one portfolio or for all of them together**: the dashboard
  (summary, change, invested, value over time), the Wallets page (balances and the registry)
  and the Operations table.
- **The web app has a portfolio selector** at the top of every page once there are two or more
  portfolios, with **All portfolios** first. The choice is remembered by the browser.
- **A Portfolios page** creates, renames and deletes portfolios, and says how many wallets and
  operations each holds.
- **Moving**: a wallet moves to another portfolio from its row on the Wallets page, and the
  operations the Operations table's filters keep move together in one step.
- Not in scope: a wallet or an operation in two portfolios at once, a share of a wallet,
  targets or allocations per portfolio, a per-portfolio balance sync (the sync stays global),
  and moving one single operation on its own (a filter narrow enough does it).

## Rulings

- **R1. One portfolio each.** A wallet and an exchange operation belong to exactly one
  portfolio. Nothing is counted twice, so *All portfolios* is the plain sum of the portfolios,
  and it is what the app showed before this change.
- **R2. An address is registered once per owner, whatever the portfolio.** The uniqueness of
  `(user_id, chain_key, address_canonical)` is unchanged. Adding an address another portfolio
  already holds is the same 409 as before; the owner moves the wallet instead.
- **R3. An operation is stored once per owner.** Deduplication stays `UNIQUE (user_id, source,
  external_id)` (spec 042, R3). Uploading a file into a second portfolio stores nothing that
  is already stored in the first, and reports those rows as already stored. To file an
  exchange's operations elsewhere, the owner moves them (R7).
- **R4. Reads take an optional `portfolio`.** `GET /api/portfolio/summary`,
  `/api/portfolio/changes`, `/api/portfolio/history`, `/api/investment`,
  `/api/exchange-operations`, `/api/wallets` and `/api/balances/current` accept
  `?portfolio=<id>`. Without it they answer for every portfolio together, exactly as before.
  An id that is not one of the caller's portfolios is a 404, the answer an id that does not
  exist gets.
- **R5. Per portfolio, everything is that portfolio's.** Held quantities, value, change and
  history come from its wallets. Invested, profit and the consistency check come from its
  operations, and its tracked assets are the assets of its active wallets (spec 042, R6).
  An operation on an asset the portfolio holds no wallet for is stored and listed, and is in
  no figure of that portfolio.
- **R6. A wallet's history moves with it.** Moving a wallet moves its snapshots and rebuilt
  balances too: the portfolio's chart is drawn as if the wallet had always been there. The
  history belongs to the address, not to the group it is filed in.
- **R7. Moving operations is by filter.** `POST /api/exchange-operations/move` takes the same
  filters as the listing (`portfolio`, `asset`, `venue`, `since`, `until`) and a target
  `portfolio_id`, and moves every operation they keep, imported and manual alike. It answers
  how many moved. Moving into the portfolio they are already in moves nothing.
- **R8. A write that names no portfolio goes to the first one**, the owner's oldest. When the
  owner has none yet, one named `Main` is created then. Creating a wallet, uploading an export
  and entering an operation by hand each take an optional `portfolio_id`.
- **R9. Names.** A name is trimmed, 1 to 60 characters, and unique per owner exactly as typed.
  A duplicate is a 409.
- **R10. Deleting.** A portfolio can be deleted only when it holds no wallet, archived ones
  included, and no operation, and when it is not the owner's last one. Otherwise 409, saying
  which. Nothing is ever deleted along with a portfolio.
- **R11. The selection is the browser's.** It is kept in `localStorage` and is *All
  portfolios* until changed. A remembered portfolio that no longer exists falls back to *All
  portfolios*. With a single portfolio the selector is not shown and every read is *All*,
  which is that portfolio.
- **R12. Forms name the portfolio when there is a choice.** With two or more portfolios, the
  wallet form, the upload and the manual entry carry a *Portfolio* select, which starts on the
  selected portfolio, or on the first one under *All portfolios*.
- **R13. The sync, the health page and the backups stay global.** They are about the data
  underneath, not about any one portfolio.

## API contract

```
GET    /api/portfolios
200 {"portfolios": [{"id": 1, "name": "Main", "wallets": 3, "operations": 912,
                     "created_at": "…Z"}]}                       oldest first
POST   /api/portfolios          {"name": "Retirement"}          201 the portfolio; 409 duplicate
PATCH  /api/portfolios/{id}     {"name": "Trading"}             200; 404; 409 duplicate
DELETE /api/portfolios/{id}                                      204; 404; 409 not empty or last

POST   /api/exchange-operations/move
       {"portfolio_id": 2, "from_portfolio": 1 | null, "asset": "KAS" | null,
        "venue": "BingX" | null, "since": "…Z" | null, "until": "…Z" | null}
200 {"moved": 41}; 404 when either portfolio is not the caller's

Optional `portfolio_id` in POST /api/wallets, POST /api/exchange-operations/imports and
POST /api/exchange-operations; `portfolio_id` in PATCH /api/wallets/{id} moves the wallet.
`portfolio_id` is served on every wallet and every operation.
```

`wallets` and `operations` are counts, not money. Every path requires a session (none is in
`PUBLIC_API_PATHS`).

## Data model

`portfolios(id, user_id → users ON DELETE CASCADE, name TEXT, created_at UtcDateTime,
updated_at UtcDateTime)`, `UNIQUE (user_id, name)`.

`wallets.portfolio_id` and `exchange_operations.portfolio_id`: `INTEGER NOT NULL →
portfolios.id`, indexed, with no cascade: a portfolio that still holds rows cannot be deleted
(R10), and the database refuses it again if a writer skips the service.

Migration `0018_portfolios` creates the table, gives every owner who has a wallet or an
operation, or who exists at all, one portfolio named `Main`, files every wallet and every
operation in their owner's, and only then makes the columns `NOT NULL`. The downgrade drops
the columns and the table, and loses only the grouping.

## Acceptance criteria

1. After the migration, every existing wallet and operation is in its owner's `Main`, and
   every figure read without `portfolio` is unchanged.
2. Portfolios can be created, listed with their counts, renamed and deleted by R9 and R10.
3. Each read in R4 narrows to the given portfolio, and refuses another owner's or an unknown
   one with 404.
4. A wallet moves with its history (R6), and a filter's operations move together (R7).
5. The invested figure and the consistency check of a portfolio use only its operations and
   its wallets (R5).
6. The selector at the top of every page switches it between the portfolios and
   *All portfolios*, and remembers the choice. The Portfolios page renders loading, empty, error and success.
7. Every new endpoint answers 401 without a session.
8. The migration reverses on its own. Every gate holds.

## Risks

- **A wallet in the wrong portfolio silently shifts value between them.** Nothing can check
  that, but nothing is lost either: the sum stays the same and the wallet can be moved back.
- **Moving by filter moves more than meant** when the filter is wider than it looks. The
  control says how many operations will move before it runs, and moving them back is the
  same action.
