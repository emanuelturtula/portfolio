# 037 — The portfolio's value over time, with past prices backfilled

Issue: none; the owner's plan of 2026-10-08 (PR 3 of 5)
Status: in progress

## Problem

The dashboard says what the wallets are worth now. It cannot say what they were worth last
week, because the application keeps one current price per pair (`prices`) and overwrites it
every hour. The balance snapshots have a history; the prices do not.

## Scope / Non-goals

- **`price_history`**: one row per asset, quote currency and UTC day, with the price of that
  day and its **basis**: `close` (the day's closing price, from a daily candle) or
  `observed` (the price the hourly refresh saw that day, the latest one).
- **The hourly refresh records `observed` rows** as it already fetches, so the history grows
  from the first deploy whatever happens to the backfill.
- **A Kraken OHLC backfill** fills in the daily closes Kraken still serves (720 days), on a
  timer of its own (`price-backfill`, daily) and by hand (`python -m portfolio
  backfill-prices`).
- **`domain/portfolio_history.py`**, pure: the value of each day from the balances held at its
  end and that day's prices. **A day nothing can value is `null`, never `0`.**
- **`GET /api/portfolio/history?range=`** and **`GET /api/wallets/{wallet_id}/value-history?range=`**.
- **The dashboard draws it**: a recharts area chart, a range selector, and the four states.
- Not in scope: balances before the first snapshot (PR 4 rebuilds them from transaction
  history), prices older than Kraken's 720 days (PR 4, from Coinbase candles), EUR history
  (the dashboard is USDT; the backfill reads USD only).

## Verified vendor facts (Kraken OHLC)

Read from Kraken's own documentation on 2026-10-08
(`https://docs.kraken.com/api-reference/market-data/get-ohlc-data`) and measured against the
live API the same day. Recorded in `docs/providers.md`.

- `GET https://api.kraken.com/0/public/OHLC?pair=<code>&interval=1440`, no key.
- Each entry is `[int <time>, string <open>, string <high>, string <low>, string <close>,
  string <vwap>, string <volume>, int <count>]`. Prices are strings.
- "Returns up to 720 of the most recent entries (older data cannot be retrieved, regardless
  of the value of `since`)."
- "The last entry in the OHLC array is for the current, not-yet-committed timeframe, and will
  always be present." `result.last` is the time of the last committed entry.
- Measured: `XXBTZUSD` returned 721 entries from 2024-10-18; `KASUSD` 689 from 2024-11-19,
  KAS's first day on Kraken. Every entry's time is 00:00:00 UTC: the open of its day.
- Rate limits are documented per API key ("call counter", 15–20); public calls are not
  addressed. Not measured. Two calls a day is far inside any reading of it, and the shared
  transport's one-request-per-second-per-host floor applies.

## Rulings

- **R1. A candle's day is the UTC date of its open time**, and its `close` is that day's
  closing price. The uncommitted last entry is never stored: it is a price still moving.
  An entry whose time is not a UTC midnight is refused.
- **R2. `close` beats `observed`, never the reverse.** The refresh writes `observed` only
  where no `close` exists for that day, and overwrites an earlier `observed` of the same day
  (the latest price seen that day is the nearest thing to a close). The backfill writes
  `close` over anything.
- **R3. A day's balance is its closing balance**: each wallet's latest snapshot observed
  before the next UTC midnight. A wallet with no snapshot by then adds nothing to that day,
  because nothing is known about it yet.
- **R4. A day's value is `null`** when no wallet had a reading by its end, or when any wallet
  holding a non-zero balance that day has no price for its asset on that day. A partial sum
  would be believed as the portfolio's value; a gap is not.
- **R5. Today** uses the latest snapshots and today's `observed` price.
- **R6. Ranges** are `30d`, `90d`, `1y` (365 days) and `all` (from the first day any active
  wallet has a snapshot). Every day of the range is a point, so a gap is visible as a gap.
  The range ends today (UTC).
- **R7. Active wallets only**, the set the summary values.
- **R8. Money stays `Decimal`** and travels as a string. Per-day closing snapshots are chosen
  in SQL by ordering on `observed_at` and `id`, never on a money column.

## API contract

```
GET /api/portfolio/history?range=90d
200 {"range": "90d", "points": [{"day": "2026-07-11", "value": "1234.5…" | null}, …]}

GET /api/wallets/{wallet_id}/value-history?range=30d
200 {"wallet_id": 1, "asset": "BTC", "range": "30d",
     "points": [{"day": "…", "quantity": "0.4" | null, "value": "…" | null}, …]}
404 for a wallet that is not the owner's.
```

Both require a session (not in `PUBLIC_API_PATHS`). Neither asks a vendor anything.

## Data model

`price_history(id, asset_id → assets, quote_currency ∈ {EUR, USD}, day DATE,
amount NumericText(12), basis ∈ {close, observed}, source TEXT, recorded_at UtcDateTime)`,
`UNIQUE (asset_id, quote_currency, day)`. Migration `0013_price_history`.

## Acceptance criteria

1. The migration creates the table with its named constraints; the drift check passes.
2. A refresh records today's `observed` price, never over a `close`.
3. The backfill stores every committed daily close Kraken returns for BTC/USD and KAS/USD,
   skips the uncommitted entry, and is idempotent.
4. The domain function returns one point per day, `null` for R4's days, and the exact sum
   otherwise.
5. Both endpoints answer 401 without a session, and the wallet one 404 for a stranger's wallet.
6. The chart renders loading, empty, error and success, and draws a gap as a gap.
7. Every gate holds: backend 99.7, domain 95/90, frontend 100 %, diff coverage 90 %.

## Risks

- **Kraken's 720 days are a rolling window**: a day older than that can only come from the
  backfill having run while it was inside it. The daily timer keeps the history complete
  from now on; older days wait for PR 4.
- **A wallet added today has no history before today**, so the chart understates the past
  until PR 4. R3 and R4 make that an honest shape rather than a wrong number: the line starts
  where the knowledge starts.
