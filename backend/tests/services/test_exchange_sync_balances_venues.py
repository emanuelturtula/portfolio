"""#104 end to end: the real Bitget and BingX providers through the real exchange sync.

`test_exchange_sync_balances.py` drives the sync against `SimulatedVenue`, which hands back
`AssetBalance`s it was given. `test_bitget_balances.py` and `test_bingx_balances.py` drive
each provider on its own. Neither shows the two meeting: that the sync asks the *real*
provider for its balances after the fills, that the venue's body -- text, signed for, parsed,
totalled -- is what ends up in `exchange_balances`, and that a venue's refusal of the balance
read alone leaves the fills imported and the account `ok`.

Here both run together:

* `BitgetProvider` and `BingXProvider`, signing every request, over the two fake venues
  (`bitget_harness.FakeBitget`, `bingx_harness.FakeBingX`), which verify every signature
  with the standard library and refuse an undocumented path;
* the real `ExchangeSyncService` and repositories, on a migrated SQLite **file** under
  `tmp_path`, read back over a second session, so what is asserted was committed.

## The figures, by hand

Bitget (`available + frozen + locked`, `limitAvailable` never added, the coin upper-cased):

| coin | available | frozen | locked | limitAvailable | stored |
|---|---|---|---|---|---|
| `kas` | 1000 | 500 | 0.5 | 999 | `KAS` 1500.5 |
| `usdt` | 100.1 | 0.2 | 0 | 0 | `USDT` 100.3 |
| `btc` | 0 | 0 | 0 | 5 | dropped: a zero |

BingX (`free + locked`, each decoded from its float spelling to fifteen significant digits):

| asset | free | locked | stored |
|---|---|---|---|
| `KAS` | 1500.5 | 0.25 | 1500.75 |
| `USDT` | 244.18616265388994 | 0 | 244.18616265389 |
| `VST` | 0 | 0 | dropped: a zero |
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio.domain.exchanges import ExchangeKey
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcomeStatus,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.services.accounting import build_accounting_service
from portfolio.services.exchange_sync import build_exchange_sync_service
from portfolio.services.reconciliation import build_reconciliation_service
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    BALANCE_STATE_SQL,
    BALANCES_SQL,
    RecordingSleeper,
    SettableClock,
    TickingMonotonic,
    rows,
    sqlite_timestamp,
)
from tests.providers.exchanges import bingx_harness, bitget_harness
from tests.providers.exchanges.bingx_harness import FakeBingX, VenueBalance
from tests.providers.exchanges.bitget_harness import FakeBitget, asset_entry
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import ExchangeProvider
    from portfolio.repositories.exchange_sync_runs import ExchangeSyncRunSummary

T0: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TODAY: Final = date(2026, 9, 25)
#: `date -u -d 2026-09-25T12:00:00Z +%s` is 1790337600.
T0_MS: Final = 1790337600000
MIDNIGHT_MS: Final = 1790294400000

BITGET_ASSETS: Final = (
    asset_entry("kas", available="1000", frozen="500", locked="0.5", limit_available="999"),
    asset_entry("usdt", available="100.1", frozen="0.2"),
    asset_entry("btc", limit_available="5"),
)
BINGX_BALANCES: Final = (
    VenueBalance("KAS", free="1500.5", locked="0.25"),
    VenueBalance("USDT", free="244.18616265388994", locked="0"),
    VenueBalance("VST", free="0", locked="0"),
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        async with built() as session:
            await insert_user(session)
        yield built


async def sync_both(
    factory: async_sessionmaker[AsyncSession],
    bitget: FakeBitget,
    bingx: FakeBingX,
    trigger: SyncTrigger = SyncTrigger.SCHEDULED,
    *,
    now: datetime = T0,
) -> ExchangeSyncRunSummary:
    """One run of the real service, with both real providers, each on its fake venue."""
    async with (
        bitget_harness.bitget_client(bitget) as bitget_http,
        bingx_harness.bingx_client(bingx) as bingx_http,
        factory() as session,
    ):
        providers: dict[ExchangeKey, ExchangeProvider] = {
            ExchangeKey.BITGET: bitget_harness.bitget_provider(
                bitget_http, clock=bitget_harness.FixedClock(now)
            ),
            ExchangeKey.BINGX: bingx_harness.bingx_provider(
                bingx_http, clock=bingx_harness.FixedClock(now)
            ),
        }
        service = build_exchange_sync_service(
            session,
            providers=providers,
            clock=SettableClock(now),
            monotonic=TickingMonotonic(),
            sleep=RecordingSleeper(),
            history_start=TODAY,
        )
        return await service.sync(trigger)


async def stored(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, Decimal]]:
    return [
        (str(row["exchange_key"]), str(row["asset"]), Decimal(str(row["quantity"])))
        for row in await rows(factory, BALANCES_SQL)
    ]


async def state(factory: async_sessionmaker[AsyncSession]) -> dict[str, dict[str, Any]]:
    return {str(row["exchange_key"]): row for row in await rows(factory, BALANCE_STATE_SQL)}


def a_bingx_fill() -> bingx_harness.VenueFill:
    """One fill inside the day's window, so the BingX account has something to import."""
    return bingx_harness.VenueFill(trade_id=41_000_001, executed_ms=MIDNIGHT_MS + 60_000)


async def test_both_real_providers_read_their_balances_after_their_fills_and_store_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The venue's text, signed for, parsed and totalled, is what the table holds."""
    bitget = FakeBitget(assets=BITGET_ASSETS)
    bingx = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES)

    summary = await sync_both(factory, bitget, bingx)

    assert summary.status is SyncRunStatus.SUCCESS
    assert [outcome.status for outcome in summary.accounts] == [AccountOutcomeStatus.SUCCESS] * 2
    assert await stored(factory) == [
        ("bingx", "KAS", Decimal("1500.75")),
        ("bingx", "USDT", Decimal("244.18616265389")),
        ("bitget", "KAS", Decimal("1500.5")),
        ("bitget", "USDT", Decimal("100.3")),
    ]
    states = await state(factory)
    for key in ("bitget", "bingx"):
        assert states[key]["balances_read_at"] == sqlite_timestamp(T0), key
        assert states[key]["balances_error"] is None, key
        assert states[key]["sync_status"] == "ok", key
    assert bitget.signature_failures == []
    assert bingx.signature_failures == []


async def test_each_venue_is_asked_for_its_balances_once_and_last(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One balance request per venue, after every fills request of that venue."""
    bitget = FakeBitget(assets=BITGET_ASSETS)
    bingx = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES)

    await sync_both(factory, bitget, bingx)

    assert len(bitget.asset_requests) == 1
    assert bitget.fill_requests, "the premise: the fills were asked for"
    assert bitget.requests[-1] is bitget.asset_requests[0], "the balance read came last"
    assert bitget.requests[-1].url.raw_path == b"/api/v2/spot/account/assets?assetType=hold_only"
    assert len(bingx.balance_requests) == 1
    assert bingx.requests, "the premise: the fills were asked for"
    assert bingx.all_requests[-1] is bingx.balance_requests[0], "the balance read came last"
    (query,) = bingx.balance_queries()
    assert query.startswith(f"timestamp={T0_MS}&signature=")
    assert bingx.balance_requests[0].url.path == "/openApi/spot/v1/account/balance"


async def test_the_stored_balances_are_what_the_holdings_check_compares(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """From two venues' HTTP bodies to one row per asset in the comparison.

    No snapshot is planted, so the history side is whatever the recompute makes of the one
    BingX fill; what is asserted is the exchange side: KAS summed over both venues,
    1500.5 + 1500.75 = 3001.25, and neither stablecoin balance a row.
    """
    bitget = FakeBitget(assets=BITGET_ASSETS)
    bingx = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES)
    await sync_both(factory, bitget, bingx)
    async with factory() as session:
        await build_accounting_service(session, clock=SettableClock(T0)).recompute(1)

    async with factory() as session:
        service = build_reconciliation_service(session, clock=SettableClock(T0))
        view = await service.reconciliation(1)

    by_asset = {row.asset: row for row in view.assets}
    assert by_asset["KAS"].exchange_quantity == Decimal("3001.25")
    assert "USDT" not in by_asset
    assert [
        (source.exchange_key, source.balances_error, source.not_compared_reason)
        for source in view.exchanges
    ] == [
        (ExchangeKey.BINGX, None, None),
        (ExchangeKey.BITGET, None, None),
    ], "a reading the sync has just stored, on an account it has just synced, is current"


async def test_a_venue_refusing_only_the_balance_read_keeps_its_fills_and_is_not_asked_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """BingX answers the fills and refuses the balance read: `100004`, permission denied.

    The fills are imported and the account is `ok`; the refusal is recorded as
    `insufficient_scope`. A second scheduled run does not send the balance request at all;
    a manual run does, and a venue that now answers clears the refusal.
    """
    refused = bingx_harness.Reply(body=bingx_harness.error_body(100004, "Permission denied"))
    first = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES, balance_replies=[refused])

    summary = await sync_both(factory, FakeBitget(assets=BITGET_ASSETS), first)

    by_key = {outcome.exchange_key: outcome for outcome in summary.accounts}
    assert summary.status is SyncRunStatus.SUCCESS
    assert by_key[ExchangeKey.BINGX].status is AccountOutcomeStatus.SUCCESS
    assert by_key[ExchangeKey.BINGX].fills_inserted == 1
    assert by_key[ExchangeKey.BINGX].error_kind is None
    states = await state(factory)
    assert states["bingx"]["balances_error"] == "insufficient_scope"
    assert states["bingx"]["balances_read_at"] is None
    assert states["bingx"]["sync_status"] == "ok"
    assert states["bitget"]["balances_error"] is None
    assert [entry[0] for entry in await stored(factory)] == ["bitget", "bitget"]
    assert len(first.balance_requests) == 1, "a refused key is not retried within the run"

    scheduled = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES)
    await sync_both(factory, FakeBitget(assets=BITGET_ASSETS), scheduled)

    assert scheduled.balance_requests == [], "a timer asked a venue that refused the key again"
    assert (await state(factory))["bingx"]["balances_error"] == "insufficient_scope"

    manual = FakeBingX([a_bingx_fill()], balances=BINGX_BALANCES)
    await sync_both(factory, FakeBitget(assets=BITGET_ASSETS), manual, SyncTrigger.MANUAL)

    assert len(manual.balance_requests) == 1
    assert (await state(factory))["bingx"]["balances_error"] is None
    assert ("bingx", "KAS", Decimal("1500.75")) in await stored(factory)


async def test_an_answer_a_provider_cannot_read_is_a_recorded_schema_failure_and_nothing_else(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bitget answers `data: null` and BingX an asset named twice: each a `schema` failure
    on the balance read, with the fills untouched and the run a success. The risk the spec
    names -- neither endpoint has been called with a real key -- ends here, as a recorded
    kind and never as a wrong number."""
    bitget = FakeBitget(asset_replies=[bitget_harness.Reply(body=bitget_harness.envelope("null"))])
    bingx = FakeBingX(
        [a_bingx_fill()],
        balances=(VenueBalance("KAS", free="1"), VenueBalance("KAS", free="2")),
    )

    summary = await sync_both(factory, bitget, bingx)

    assert summary.status is SyncRunStatus.SUCCESS
    assert [outcome.status for outcome in summary.accounts] == [AccountOutcomeStatus.SUCCESS] * 2
    states = await state(factory)
    assert states["bitget"]["balances_error"] == "schema"
    assert states["bingx"]["balances_error"] == "schema"
    assert await stored(factory) == []
