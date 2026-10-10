# 042 — Exchange operations from CSV exports: invested, profit and loss, and a consistency check

Issue: none; the owner's request of 2026-10-10
Status: in progress

## Problem

The dashboard says what the wallets are worth. It cannot say how much money went in, so it
cannot say whether the portfolio is up or down. Spec 036 removed the API-driven version of
that answer along with its credentials and vendors. The owner now downloads each exchange's
history by hand (spec 040) and wants those files to be the source instead.

Every coin in the wallets was bought on one of four exchanges (Bitget, BingX, Binance and Nexo,
whose download also carries the owner's Buenbit history) or swapped in the Tangem app. So the
net quantity those operations produced should equal what the wallets hold, and a difference
means a report is missing or coins went somewhere else.

## Scope / Non-goals

- **`exchange_operations`**: one row per operation in an uploaded export, **for every asset**
  (USDT, NEXO, futures and the rest as well as BTC and KAS). The owner asked for the whole
  history to be kept. Only the figures filter.
- **Upload in the browser**: several files at once are sent one request each, in order. A
  file the server refuses stops the run there; the files before it stay stored.
- **Upload**: `POST /api/exchange-operations/imports` takes one file as the exchange serves it
  (a CSV, or a zip of CSVs), recognises its format by its header row, and stores every
  operation not already stored. Uploading the same file again stores nothing.
- **Manual operations**: `POST /api/exchange-operations` records a buy or a sell the exports do
  not cover (the Tangem swaps), and `DELETE` removes one. Imported rows cannot be deleted.
- **`GET /api/exchange-operations`**: the stored operations, newest first, paged.
- **`GET /api/investment`**: per tracked asset and in total, the net invested in USDT, the value
  now, the profit or loss, the quantity the operations explain beside the quantity held, and
  the cumulative invested per day for the chart.
- **Web**: an Operations page (upload, manual entry, the table), an Invested section on the
  dashboard, and an Invested line on the value chart.
- Not in scope: editing an imported row, deleting an import, prices for a trade paid in a
  non-stable currency, futures profit, taxes, EUR.

## Verified export formats

Read from the owner's own exports downloaded on 2026-10-08 and 2026-10-09, the only
documentation these files have. No vendor API is called.

| Format | Recognised by its header | Row id | Clock |
|---|---|---|---|
| Bitget spot order details | `Date,Trading pair,Base Asset,Quote Asset,Direction,Price,Amount,Total,Fee,Fee Coin` | none | UTC-3, unmarked |
| Bitget spot transactions | `order,Date,Coin,Type,Amount,Fee,Available` | `order` (leading TAB) | UTC-3, unmarked |
| BingX spot order history | `UID,Order No.,Time(<zone>),Pair,Type,Price,Amount,Order Value,Fee,Fee Coin,Order Type` | `Order No.` | the zone named in the header |
| BingX perpetual futures order history | `UID,Order No.,Time(<zone>),Pair,Type,Leverage,…,Realized PNL,…` | `Order No.` | the zone named in the header |
| BingX fund account | `UID,type,amount,new_available_amount,asset_name,Time(<zone>),remark` | none | the zone named in the header |
| Binance transaction history | `User ID,Time,Account,Operation,Coin,Change,Remark` | none | named in the file name, `(UTC-3)` |
| Nexo transactions | `Transaction,Type,Input Currency,Input Amount,Output Currency,Output Amount,USD Equivalent,Fee,Fee Currency,Details,Date / Time (UTC)` | `Transaction` | UTC |
| Buenbit (inside Nexo's download) | `FECHA,ID,OPERACION,ESTADO,MONEDA,MONTO,COSTO DE RED,TXID` | `ID` + `MONEDA` | UTC-3, unmarked |

Confirmed facts the parsers rest on:

- **Bitget's clock is UTC-3.** A BTC withdrawal at `2026-04-01 07:08:12` arrives in Nexo, which
  writes UTC, at `10:30:34`. Three hours and the network's confirmation time.
- **Bitget's order details carry legitimately identical rows**: 15 fills with the same second,
  price, amount and fee. Deduplicating by content alone would drop real trades.
- **Bitget's spot transactions** separate the withdrawal fee (`Fee`) from the amount sent
  (`Amount`). The ledger's `Buy` and `Sell` lines duplicate the order details and are not read
  from it.
- **BingX's spot ledger** (`Time,type,Amount,newAvailableAmount,Assets`) repeats the order
  history's trades without ids and is not read. The order history's fee, negative and in the
  coin received, is on top of the order's `Amount`, not taken out of it.
- **Binance names its clock only in the file name**, `…(UTC-3).csv`. A Binance file whose name
  lost that suffix is refused, saying so, rather than read in a guessed zone. Its spot trade
  legs (`Transaction Buy`, `Transaction Spend`, `Transaction Fee`) carry no counterpart on their
  line and are stored as `other`; the owner's BTC and KAS did not come from them.
- **Nexo's** `Exchange` input includes the fee, and its `Withdrawal` input is the output plus the
  fee. `Interest` is paid in NEXO even when `Details` names another coin's equivalent.
- **Buenbit** writes a conversion on two lines: the first has the date, the id and what came in;
  the next line has no date or id and holds what went out. Amounts use a comma for thousands.
  Interest rows reuse one id per day for every currency. A second section, after a line of
  dashes, holds stock operations with other columns and is not read.
- **Known and deliberately not read**, each reported as skipped with its reason: Bitget's spot
  order history (its fills are in the order details), Bitget's deposit and withdrawal records
  (the spot transactions carry them with the fee), and BingX's spot and futures ledgers (the
  order histories carry them, with ids).

## Rulings

- **R1. One row per source operation.** `kind` is `buy`, `sell`, `reward`, `deposit`,
  `withdrawal`, `transfer` or `other`; `description` keeps the source's own type, such as
  `P2P Trading` or `Open Long`. A trade is a `buy` of the coin received paid in the coin given
  (Buenbit and Nexo conversions included), except when only the coin received is a
  stablecoin: then it is a `sell` of the coin given, so `KAS -> USDT` is a sell of KAS and
  never a buy of USDT. An exchange's sell order is a `sell` of its base coin.
- **R2. Quantities.** `quantity` is what the operation moved before its fee; `fee_amount` in
  `fee_asset` is the fee on top. The parsers bring each format to this. Nexo's `Exchange` and
  `Withdrawal` inputs, which include the fee, have it subtracted.
- **R3. Deduplication is `UNIQUE (source, external_id)`.** `external_id` is the source's own id
  where it has one (Buenbit's joined with the currency). Where it has none, it is the format
  and a hash of the row's fields; the second identical row of a file gets `#2`, the third `#3`.
  A manual entry's id is `manual:` and a random UUID. An export
  always holds every row of a second, so the same row gets the same id in every export that
  contains it. A stored id is skipped, never overwritten.
- **R4. A file is recognised by its header, never by its name.** A file in a zip whose header
  is unknown, or is a known format this importer does not read, is reported as skipped with
  its count of data rows, never silently dropped. An unreadable row in a recognised file refuses the
  whole upload, naming the file and the line, and nothing is stored.
- **R5. Every time is stored in UTC**, converted from the clock the format uses.
- **R6. Tracked assets** are the assets of the owner's active wallets. Only their operations
  enter the figures, and every operation is stored.
- **R7. Invested.** Per tracked asset, the sum of what its buys cost minus what its sells
  brought in, in USDT. A fee charged in the quote currency is part of what a buy cost, and
  comes out of what a sell brought in. USDT, USDC and DAI count one for one. A buy or sell paid in any other
  currency makes the asset's invested figure **unknown** (`unvalued_trades` counts them). It is
  never zero or partial.
- **R8. Explained quantity** = buys − sells + rewards − every fee charged in the asset (trading
  fees and withdrawal fees). Deposits, withdrawals and transfers move coins between places the
  owner controls and do not change it. **Difference** = held − explained.
- **R9. Profit or loss** = the asset's value now (the dashboard summary's, so they cannot
  disagree) minus its invested figure. The percentage is that over the invested figure, times
  100, rounded half-even to four places. With either figure unknown, the profit is **null with
  the reason**, never `0`. The total is the sum over tracked assets, and is unknown when any of
  them is.
- **R10. Cumulative invested per day** is the total invested after every trade of tracked
  assets up to the end of that UTC day. The series has one step for each day that changed it.
  It stops being known from the first day an unvalued trade happens.
- **R11. Manual operations** have `source = manual` and a `venue` the owner names (`Tangem`).
  Only `buy` and `sell` can be entered by hand (spec 043 adds `reward` and `fee`). Only manual
  rows can be deleted.
- **R12. Uploads travel as JSON** (`{"filename", "content_base64"}`). The write guard only lets
  JSON change state, and that stays the rule. The decoded file may be at most 5 MiB, and a zip
  may hold at most 100 files and 50 MiB uncompressed.

## API contract

```
POST /api/exchange-operations/imports  {"filename": "x.zip", "content_base64": "…"}
201 {"filename": "x.zip", "files": [{"name": "…", "format": "bitget_spot_order_details" | null,
      "rows": 218, "stored": 218, "already_stored": 0, "skipped_reason": null | "…"}],
     "stored": 218, "already_stored": 0}
422 problem document naming the file and line that could not be read

GET  /api/exchange-operations?limit=100&offset=0
200 {"count": 912, "operations": [{"id", "source", "venue", "external_id", "executed_at",
      "kind", "asset", "quantity", "quote_currency", "quote_amount", "fee_asset",
      "fee_amount", "description", "manual"}]}

POST /api/exchange-operations  {"venue": "Tangem", "executed_at": "…Z", "kind": "buy",
      "asset": "KAS", "quantity": "6100", "quote_currency": "USDT",
      "quote_amount": "500", "description": "…"}
201 the stored operation
DELETE /api/exchange-operations/{id}  204; 404 when absent; 409 when not manual

GET  /api/investment
200 {"assets": [{"asset": "KAS", "invested": "…" | null, "value": "…" | null,
      "pnl": "…" | null, "pnl_pct": "…" | null, "held": "…", "explained": "…",
      "difference": "…", "trades": 230, "unvalued_trades": 0,
      "unavailable": null | "unvalued_trades" | "value_unknown" | "nothing_invested"}],
     "overall": {"invested", "value", "pnl", "pnl_pct", "unavailable"},
     "invested_by_day": [{"day": "2025-05-11", "invested": "…" | null}]}
```

Every amount is a JSON string. Every path requires a session (none is in `PUBLIC_API_PATHS`).
The reasons are the `InvestmentUnavailable` enum. The list's size is `count` and the totals
are `overall`, because a property named `total` is read as money by the schema's money test.

## Data model

`exchange_operations(id, user_id → users, source TEXT, venue TEXT, external_id TEXT,
executed_at UtcDateTime, kind TEXT CHECK, asset TEXT, quantity NumericText(18),
quote_currency TEXT NULL, quote_amount NumericText(18) NULL, fee_asset TEXT NULL,
fee_amount NumericText(18) NULL, description TEXT, import_id → exchange_imports NULL,
created_at UtcDateTime)`, `UNIQUE (user_id, source, external_id)`.

`exchange_imports(id, user_id → users, filename TEXT, sha256 TEXT, stored INTEGER,
already_stored INTEGER, imported_at UtcDateTime)`.

`asset` is the symbol as text, not a key into `assets`, because most stored rows are coins the
portfolio does not track. Migration `0016_exchange_operations`.

## Acceptance criteria

1. Each format above parses its sample (synthetic, testnet-only addresses) into the expected
   operations, in UTC, with R2's quantities.
2. Bitget's identical fills get distinct ids, and the same file uploaded twice stores nothing
   the second time.
3. A zip with an unknown CSV reports it as skipped. A malformed row refuses the upload whole.
4. Invested, explained, difference and profit follow R7 to R9 exactly, including every unknown
   case, and never render an unknown as `0`.
5. Every new endpoint answers 401 without a session, and a non-JSON upload is refused.
6. The Operations page renders loading, empty, error and success; the upload reports what was
   stored and skipped; a manual operation can be added and deleted.
7. The dashboard's Invested section shows invested, value and profit with a sign and a word,
   not only a colour, and each asset's held and explained quantities with the difference.
8. Every gate holds.

## Risks

- **Formats change without notice.** An unknown header is reported, not guessed. A changed
  column breaks loudly at upload rather than storing wrong numbers.
- **Content ids depend on exports never splitting a second.** If a venue ever cut an export in
  the middle of a second, one of its identical fills could be stored twice. Ids from the venue
  are used wherever a format has one.
- **The exports hold account ids and withdrawal addresses.** They are stored in `description`
  only as the venue's type, never the remark, and tests use synthetic rows with testnet
  addresses (rule 3).
- **Spec 040's open pull request adds a `0015` revision too**, and this one's `0016` sits on
  spec 041's `0015_price_hourly`. Whichever merges second moves its revision on top of the
  other.
