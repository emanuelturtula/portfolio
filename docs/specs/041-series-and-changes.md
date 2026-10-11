# 041 — Each asset over time, and the change over 24 hours and 7 days

Issue: none; the owner's request of 2026-10-10
Status: in progress

## Problem

The value-over-time chart draws one line: every wallet together. It cannot say whether a rise
came from BTC or from KAS, and an owner who wants to know has to open each wallet on the
Wallets page and compare two charts by eye.

The dashboard says what the portfolio is worth now, and the chart says what it was worth at
the end of each past day. Neither says how much it moved in the last 24 hours or the last
week, which is the first question asked of a portfolio.

## Scope / Non-goals

- **The portfolio history carries each asset's value per day** beside the total, and the chart
  draws any combination of the total and the assets, chosen with toggles. The total alone is
  the default, so the chart looks as it did until someone asks for more.
- **A change widget on the dashboard**, under the hero figure: the change over the last 24
  hours and over the last 7 days, each as an amount in USDT and a percentage, green for a
  rise and red for a fall, and always with a sign.
- **`price_hourly`**: one row per asset, quote currency and UTC hour, holding the hour's
  closing price from Kraken's hourly candles. The value 24 hours ago needs the price 24 hours
  ago, and `price_history` has one price per day.
- **The hourly price timer also stores the hourly closes** Kraken serves for BTC/USD and
  KAS/USD, so the table is filled from the first run, 30 days back, and stays current.
- **`GET /api/portfolio/changes`**, read-only, like every other dashboard endpoint.
- Not in scope: a change for a single wallet or asset, any other period, EUR, intraday charts.

## Verified vendor facts (Kraken hourly OHLC)

The same endpoint spec 037 verified, with another documented interval. Read from Kraken's own
documentation on 2026-10-10 (`https://docs.kraken.com/api-reference/market-data/get-ohlc-data`)
and measured against the live API the same day. Recorded in `docs/providers.md`.

- `GET https://api.kraken.com/0/public/OHLC?pair=<code>&interval=60`, no key. `60` is one of
  the documented intervals (1, 5, 15, 30, 60, 240, 1440, 10080, 21600).
- "Returns up to 720 of the most recent entries"; "the last entry in the OHLC array is for the
  current, not-yet-committed timeframe, and will always be present". `result.last` is the open
  time of the last committed entry.
- Measured: `XXBTZUSD` and `KASUSD` each returned 721 entries, 30 days of hours, every one
  opening on the hour, with no missing hour; `last` was the hour before the final entry.

## Rulings

- **R1. A candle's hour is its open time**, and its close is the price at the end of that hour.
  The uncommitted last entry is never stored. An entry that does not open on the hour is
  refused. A committed candle is final, so a stored hour is never rewritten.
- **R2. The price at an instant `T`** is the close of the latest stored hour that ended at or
  before `T`, and only if it ended less than two hours before `T`. Older than that, the asset
  has no price at `T`: a stale price would make a change that never happened.
- **R3. A wallet's quantity at `T`** is its latest balance snapshot observed at or before `T`.
  Before its first snapshot, it is its rebuilt balance (spec 038) at the end of the day before
  `T`'s UTC day, and `0` when `T`'s day is not after its first rebuilt day: a rebuilt history
  is proven to start from zero. A wallet with neither is unknown at `T`.
- **R4. The value at `T`** is the sum over the active wallets of quantity at `T` times price at
  `T`. It is **unknown** when any wallet is unknown at `T`, or when any wallet holding a
  non-zero quantity at `T` has no price at `T`. A wallet left out would turn its whole balance
  into a gain or a loss.
- **R5. The value now** is the summary's total, so the widget and the hero cannot disagree. It
  is **unknown** when the summary is missing a wallet that was never read, or an asset with no
  price. A stale reading or a stale price still values, as it does in the hero, which says so.
- **R6. A change** over a period `P` is the value now minus the value at `now − P`, exact. Its
  percentage is that change over the value at `now − P`, times 100, rounded half-even to four
  places as every percentage here is (the widget shows two); with nothing held at `now − P`
  there is no percentage. When both values are unknown, the reason about the past is given. When either value is unknown
  the change is **unavailable**, with the reason, and it is never shown as `0`.
- **R7. A day's value of one asset** is the sum of its wallets' values that day, by spec 037's
  R3 and R4 applied to that asset's wallets alone: `null` when none of them had been read by
  then, or when one held a non-zero quantity with no price that day. The total is unchanged.
- **R8. The periods** are `24h` and `7d`, in that order. Active wallets only. Money stays
  `Decimal` and travels as a string.

## API contract

```
GET /api/portfolio/history?range=90d
200 {"range": "90d", "assets": ["BTC", "KAS"],
     "points": [{"day": "2026-07-11", "value": "1234.5…" | null,
                 "assets": {"BTC": "1000.0…" | null, "KAS": "234.5…" | null}}, …]}

GET /api/portfolio/changes
200 {"as_of": "2026-10-10T12:00:00Z", "value": "1234.56" | null,
     "changes": [{"period": "24h", "since": "2026-10-09T12:00:00Z",
                  "value_then": "1200.00" | null, "change": "34.56" | null,
                  "change_pct": "2.8800" | null,
                  "unavailable": null | "value_unknown_now" | "no_reading_then"
                               | "no_price_then"}, …]}
```

`assets` lists the asset of every active wallet, sorted. Both endpoints require a session (not
in `PUBLIC_API_PATHS`), and neither asks a vendor anything.

## Data model

`price_hourly(id, asset_id → assets, quote_currency ∈ {EUR, USD}, hour UtcDateTime,
amount NumericText(12), source TEXT, recorded_at UtcDateTime)`,
`UNIQUE (asset_id, quote_currency, hour)`. Migration `0015_price_hourly`.

## Acceptance criteria

1. The migration creates the table with its named constraints; the drift check passes.
2. The parser stores every committed hourly close, refuses one that is not on the hour, and
   skips the uncommitted entry; storing the same candles twice adds nothing.
3. The domain returns a change and a percentage exactly, and `unavailable` for each of R4's,
   R5's and R2's cases.
4. The history carries each asset's value per day by R7, and the total is unchanged.
5. `GET /api/portfolio/changes` answers 401 without a session.
6. The chart offers Total and each asset as toggles, draws every pressed one, and always keeps
   one pressed. Its tooltip and its accessible description name each series drawn.
7. The widget renders loading, error, and each change or its reason; a rise reads `+` and a
   fall `−`, in words as well as in colour.
8. Every gate holds: backend 99.7, domain 95/90, frontend 100 %, diff coverage 90 %.

## Risks

- **Two more Kraken calls an hour**, 1,440 a month, at the shared one-request-a-second floor.
  Nothing falls back from Kraken for them: when it fails, the change says the price is missing
  rather than guessing one.
- **Spec 040's open pull request also adds a `0015` revision.** Whichever merges second moves
  its revision on top of the other.
- **A wallet's rebuilt balance at `T` misses a transaction made earlier on `T`'s own day**
  (R3). It only applies to the hours before the wallet's first snapshot.
- **The total's line is blue, the spare slot `assetColors` hands a third asset.** Only BTC and
  KAS are tracked, and they have colours of their own; a third asset would share the total's
  hue on the chart, told apart by its toggle and its tooltip label.
