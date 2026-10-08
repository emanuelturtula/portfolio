"""Spec 038, criterion 1 on Kaspa: `KaspaProvider.address_history`.

The fake pages `full-transactions-page` the way the public instance was measured to on
2026-10-08: newest first, `limit` rows and then the rest of the boundary millisecond, and an
`X-Next-Page-Before` header carrying the page's smallest `block_time` while more rows remain.
The provider has to follow that header, never compute a cursor, and prove the history it
collected (R1) -- the distinct accepted ids number `/transactions-count`, their effects sum
to `/balance`, every input was resolved, and the count and balance agree before and after.

As for Bitcoin, the assertions that carry the weight are **which requests went where, with
which cursor**, **the reason** a history is incomplete, and **that no refusal names the
address, a transaction id or an amount**.

Every address is `kaspatest:` (rule 3), and every id is built at run time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.providers.base import (
    AddressHistory,
    HistoryIncomplete,
    TransactionHistoryReader,
    TxEffect,
)
from portfolio.providers.chains.kaspa import (
    HISTORY_PAGE_LIMIT,
    KASPA_DECIMALS,
    LATEST_BLOCK_TIME_MS,
    KaspaProvider,
    parse_next_page_before,
    parse_transaction_count,
    parse_transactions_page,
)
from portfolio.providers.errors import ProviderResponseError, ProviderUnavailableError
from portfolio.providers.http import ADDRESS_BALANCE, ADDRESS_HISTORY, ENDPOINT_EXTENSION
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V1_ZERO,
    KASPA_WRONG_NETWORK_PREFIX,
)
from tests.providers.chains.kaspa_harness import (
    BLOCK_TIME_MS,
    FALLBACK_HOST,
    NEXT_PAGE_HEADER,
    PRIMARY_HOST,
    KaspaFake,
    KaspaTx,
    Reply,
    ScriptedInstance,
    balance_body,
    history_reply,
    kaspa_provider,
    kaspa_settings,
    requested_address,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ADDRESS: Final = KASPA_TESTNET_V0
"""The address whose history is read."""
OTHER: Final = KASPA_TESTNET_V1_KEY
THIRD: Final = KASPA_TESTNET_V1_ZERO

ONE_COIN: Final = 100_000_000
DUST: Final = 54_321
REWARD: Final = 4_376_190_000

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def at(seed: int) -> int:
    """A block time 1,357 ms per seed after `BLOCK_TIME_MS`: newer seeds are later, and no
    two land on the same millisecond unless a test says so."""
    return BLOCK_TIME_MS + seed * 1_357


def when(milliseconds: int) -> datetime:
    """The exact instant of an epoch-millisecond time, computed without the provider's code."""
    return EPOCH + timedelta(milliseconds=milliseconds)


# One of each kind of effect, oldest first, every amount distinct.
COINBASE: Final = KaspaTx(seed=1, block_time=at(1), coinbase=True, outputs=((ADDRESS, REWARD),))
RECEIVED: Final = KaspaTx(
    seed=2,
    block_time=at(2),
    inputs=((OTHER, 150_000_000),),
    outputs=((ADDRESS, ONE_COIN), (OTHER, 49_990_000)),
)
SPENT: Final = KaspaTx(
    seed=3,
    block_time=at(3),
    inputs=((ADDRESS, ONE_COIN),),
    outputs=((THIRD, 60_000_000), (ADDRESS, 39_990_000)),
)
SELF_TRANSFER: Final = KaspaTx(
    seed=4,
    block_time=at(4),
    inputs=((ADDRESS, 39_990_000), (OTHER, 7_000)),
    outputs=((ADDRESS, 39_990_000),),
)
"""Nets to exactly zero: the fee is paid by another address's input."""

FOUR_KINDS: Final = (COINBASE, RECEIVED, SPENT, SELF_TRANSFER)
FOUR_KINDS_BALANCE: Final = REWARD + ONE_COIN - 60_010_000


def newest_first(transactions: Sequence[KaspaTx]) -> list[KaspaTx]:
    return list(reversed(transactions))


def ledger(count: int) -> list[KaspaTx]:
    """`count` transactions oldest first: a coinbase, then receipts and spends alternating."""
    transactions: list[KaspaTx] = []
    for seed in range(1, count + 1):
        if seed == 1:
            transactions.append(
                KaspaTx(seed=seed, block_time=at(seed), coinbase=True, outputs=((ADDRESS, REWARD),))
            )
        elif seed % 2 == 0:
            transactions.append(
                KaspaTx(
                    seed=seed,
                    block_time=at(seed),
                    inputs=((OTHER, DUST + seed + 1_000),),
                    outputs=((ADDRESS, DUST + seed),),
                )
            )
        else:
            transactions.append(
                KaspaTx(
                    seed=seed,
                    block_time=at(seed),
                    inputs=((ADDRESS, seed * 1_000 + 500),),
                    outputs=((OTHER, seed * 1_000), (ADDRESS, 300)),
                )
            )
    return transactions


def serving(transactions: Sequence[KaspaTx], **overrides: Any) -> KaspaFake:
    """One instance serving `transactions` (oldest first) and totals that agree with them."""
    reply = history_reply(ADDRESS, newest_first(transactions), **overrides)
    return KaspaFake(primary=ScriptedInstance(reply))


async def read_history(fake: KaspaFake, address: str = ADDRESS, **options: Any) -> AddressHistory:
    provider, client = kaspa_provider(fake, **options)
    async with client:
        return await provider.address_history(address)


def amounts_of(transactions: Sequence[KaspaTx]) -> set[str]:
    """Every amount in the transactions, as the digits a message could leak."""
    values = {amount for transaction in transactions for _owner, amount in transaction.outputs}
    values |= {spend[1] for transaction in transactions for spend in transaction.inputs if spend}
    return {str(value) for value in values if value >= 1_000}


# --------------------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------------------


_HISTORY: TransactionHistoryReader = KaspaProvider(httpx.AsyncClient(), settings=kaspa_settings())
"""`mypy --strict` deciding that the Kaspa provider satisfies the protocol's signature.

The client is never used, so importing this module opens no connection pool.
"""


def test_the_kaspa_provider_is_a_transaction_history_reader() -> None:
    """The run-time gate the rebuild uses; the signature is the assignment above's."""
    assert isinstance(_HISTORY, TransactionHistoryReader)


# --------------------------------------------------------------------------------------
# A complete history
# --------------------------------------------------------------------------------------


async def test_each_kind_of_transaction_has_its_net_effect_oldest_first() -> None:
    """R2, row by row: a coinbase (`inputs: null`), received, spent, and a self-transfer
    netting to zero. Oldest first, each at its block's exact millisecond, in UTC."""
    fake = serving(FOUR_KINDS)

    history = await read_history(fake)

    assert history == AddressHistory(
        address=ADDRESS,
        balance=FOUR_KINDS_BALANCE,
        decimals=KASPA_DECIMALS,
        effects=(
            TxEffect(occurred_at=when(at(1)), delta=REWARD),
            TxEffect(occurred_at=when(at(2)), delta=ONE_COIN),
            TxEffect(occurred_at=when(at(3)), delta=39_990_000 - ONE_COIN),
            TxEffect(occurred_at=when(at(4)), delta=0),
        ),
        incomplete=None,
    )
    assert all(effect.occurred_at.tzinfo is UTC for effect in history.effects)
    assert history.effects[1].occurred_at.microsecond == (at(2) % 1_000) * 1_000


async def test_the_reads_are_totals_then_pages_then_totals_again() -> None:
    """Count and balance, the page with `limit=500&resolve_previous_outpoints=light`, then
    count and balance again -- with the labels a log reader tells them apart by."""
    fake = serving(FOUR_KINDS)

    await read_history(fake)

    paths = [request.url.path for request in fake.requests]
    assert paths == [
        f"/addresses/{ADDRESS}/transactions-count",
        f"/addresses/{ADDRESS}/balance",
        f"/addresses/{ADDRESS}/full-transactions-page",
        f"/addresses/{ADDRESS}/transactions-count",
        f"/addresses/{ADDRESS}/balance",
    ]
    labels = [request.extensions.get(ENDPOINT_EXTENSION) for request in fake.requests]
    assert labels == [
        ADDRESS_HISTORY,
        ADDRESS_BALANCE,
        ADDRESS_HISTORY,
        ADDRESS_HISTORY,
        ADDRESS_BALANCE,
    ]
    (page,) = fake.history_requests()
    assert dict(page.url.params) == {
        "limit": str(HISTORY_PAGE_LIMIT),
        "resolve_previous_outpoints": "light",
    }
    assert all(request.method == "GET" for request in fake.requests)
    assert {requested_address(request) for request in fake.requests} == {ADDRESS}


async def test_the_pager_follows_the_header_across_a_completed_boundary_millisecond() -> None:
    """Four rows asked for, five served: the fourth and fifth share a millisecond.

    The cursor is the vendor's -- the page's smallest `block_time` -- and the second page is
    everything strictly before it. A pager that computed its own cursor from the fourth row
    would have asked for rows before a millisecond whose second row it never saw.
    """
    transactions = ledger(7)
    shared = at(3)
    transactions[2] = replace(transactions[2], block_time=shared)
    transactions[3] = replace(transactions[3], block_time=shared)
    fake = serving(transactions, page_size=4)

    history = await read_history(fake)

    assert history.incomplete is None
    assert len(history.effects) == 7
    first, second = fake.history_requests()
    assert "before" not in first.url.params
    assert second.url.params["before"] == str(shared)
    assert sum(effect.delta for effect in history.effects) == history.balance


async def test_a_history_over_several_full_pages_is_read_to_the_end() -> None:
    """1,001 rows at the real `limit`: 500, 500 and 1, the header absent on the last.

    Three pages against a cap of four; the cursors are the 500th and the 1,000th rows'
    times, newest first.
    """
    transactions = ledger(1_001)
    fake = serving(transactions)

    history = await read_history(fake)

    assert history.incomplete is None
    assert len(history.effects) == 1_001
    assert [effect.occurred_at for effect in history.effects] == [
        when(transaction.block_time) for transaction in transactions
    ]
    befores = [request.url.params.get("before") for request in fake.history_requests()]
    assert befores == [None, str(at(502)), str(at(2))]


async def test_an_address_with_no_history_is_complete_and_empty() -> None:
    """A zero count, a zero balance, and one empty page with no cursor: complete."""
    fake = serving(())

    history = await read_history(fake)

    assert history == AddressHistory(
        address=ADDRESS, balance=0, decimals=KASPA_DECIMALS, effects=(), incomplete=None
    )
    assert len(fake.history_requests()) == 1


async def test_an_unaccepted_row_is_left_out_of_the_history() -> None:
    """`is_accepted: false` is not a transaction this address made (R2), whatever it pays.

    The vendor's count here leaves it out too, so the rest prove themselves.
    """
    rejected = KaspaTx(seed=5, block_time=at(5), outputs=((ADDRESS, 7 * ONE_COIN),), accepted=False)
    fake = serving((*FOUR_KINDS, rejected))

    history = await read_history(fake)

    assert history.incomplete is None
    assert len(history.effects) == 4
    assert history.balance == FOUR_KINDS_BALANCE


async def test_an_unaccepted_row_the_vendor_counts_is_a_count_mismatch() -> None:
    """If `/transactions-count` counts it, the history is reported incomplete, not wrong."""
    rejected = KaspaTx(seed=5, block_time=at(5), outputs=((ADDRESS, 7 * ONE_COIN),), accepted=False)
    fake = serving((*FOUR_KINDS, rejected), tx_total=5)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH


def _pages(*pages: tuple[Sequence[KaspaTx], int | None]) -> list[Reply]:
    """Replies serving each page verbatim, with the cursor given, for repeat scenarios."""
    return [
        Reply(
            body=json.dumps([transaction.document() for transaction in rows]),
            headers={NEXT_PAGE_HEADER: str(cursor)} if cursor is not None else {},
        )
        for rows, cursor in pages
    ]


@pytest.mark.parametrize(("total", "expected"), [(3, None), (4, HistoryIncomplete.COUNT_MISMATCH)])
async def test_a_row_served_on_two_pages_is_counted_once(
    total: int, expected: HistoryIncomplete | None
) -> None:
    """A transaction repeated across a page boundary is one transaction; the count decides."""
    totals = history_reply(ADDRESS, newest_first(FOUR_KINDS[1:]), tx_total=total)
    first, second = _pages(
        ((SELF_TRANSFER, SPENT), at(3)),
        ((SPENT, RECEIVED), None),
    )
    fake = KaspaFake(primary=ScriptedInstance(totals, totals, first, second, totals))

    history = await read_history(fake)

    assert history.incomplete is expected
    assert len(history.effects) == 3


# --------------------------------------------------------------------------------------
# An incomplete history, and why
# --------------------------------------------------------------------------------------


async def test_fewer_accepted_rows_than_the_vendor_counts_is_a_count_mismatch() -> None:
    fake = serving(FOUR_KINDS, tx_total=5)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH
    assert len(history.effects) == 4


async def test_effects_that_do_not_sum_to_the_balance_are_a_balance_mismatch() -> None:
    fake = serving(FOUR_KINDS, balance=FOUR_KINDS_BALANCE + DUST)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.BALANCE_MISMATCH
    assert history.balance == FOUR_KINDS_BALANCE + DUST


@pytest.mark.parametrize(
    "moved",
    [{"tx_total": 5}, {"balance": FOUR_KINDS_BALANCE + DUST}],
    ids=["a new transaction", "a new balance"],
)
async def test_totals_that_changed_during_the_read_mean_it_moved(moved: dict[str, Any]) -> None:
    """The count or the balance read after the paging differs from the one read before.

    The balance reported is the one read before, which the effects were collected against.
    """
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    after = replace(reply, **moved)
    fake = KaspaFake(primary=ScriptedInstance(reply, reply, reply, after, after))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.MOVED_DURING_READ
    assert history.balance == FOUR_KINDS_BALANCE
    assert len(fake.requests) == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("previous_outpoint_address", None),
        ("previous_outpoint_amount", None),
        ("previous_outpoint_address", ...),
        ("previous_outpoint_amount", ...),
    ],
    ids=["address null", "amount null", "address absent", "amount absent"],
)
async def test_an_input_whose_source_is_not_resolved_is_an_unresolved_input(
    field: str, value: object
) -> None:
    """Either half of `light` resolution missing, `null` or absent: the input is unknown.

    The totals agree with the transactions as served, so the input is the only thing wrong
    and the reason names it. Its effect leaves the input out.
    """
    document = SPENT.document()
    if value is ...:
        del document["inputs"][0][field]
    else:
        document["inputs"][0][field] = value
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    rows = [
        document if row["transaction_id"] == SPENT.transaction_id else row for row in reply.rows
    ]
    fake = KaspaFake(primary=ScriptedInstance(replace(reply, rows=tuple(rows))))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.UNRESOLVED_INPUT
    assert history.effects[2] == TxEffect(occurred_at=when(at(3)), delta=39_990_000)


async def test_a_vendor_whose_cursor_never_runs_out_stops_at_the_cap() -> None:
    """Every row on every page and a cursor on every page: `total // 500 + 2` pages, then stop.

    Kept once, the rows even number `total` and sum to the balance -- so it is the missing
    end alone that makes this a count mismatch. Without the cap this test would not finish.
    """
    fake = serving(FOUR_KINDS, ignore_cursor=True, headers={NEXT_PAGE_HEADER: str(at(1))})

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH
    assert len(history.effects) == 4
    assert sum(effect.delta for effect in history.effects) == history.balance
    assert len(fake.history_requests()) == 4 // HISTORY_PAGE_LIMIT + 2


async def test_moved_during_the_read_outranks_every_other_reason() -> None:
    unresolved = KaspaTx(seed=5, block_time=at(5), inputs=(None,), outputs=((OTHER, DUST),))
    reply = history_reply(ADDRESS, newest_first((*FOUR_KINDS, unresolved)), tx_total=9)
    after = replace(reply, tx_total=10)
    fake = KaspaFake(primary=ScriptedInstance(reply, reply, reply, after, after))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.MOVED_DURING_READ


async def test_an_unresolved_input_outranks_a_count_and_a_sum_that_disagree() -> None:
    unresolved = KaspaTx(seed=5, block_time=at(5), inputs=(None,), outputs=((OTHER, DUST),))
    fake = serving((*FOUR_KINDS, unresolved), tx_total=9, balance=1)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.UNRESOLVED_INPUT


async def test_a_count_mismatch_outranks_a_sum_that_disagrees() -> None:
    fake = serving(FOUR_KINDS, tx_total=9, balance=1)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH


# --------------------------------------------------------------------------------------
# Failover and validation
# --------------------------------------------------------------------------------------


async def test_failover_mid_history_is_sticky_and_carries_the_cursor() -> None:
    """The primary answers the totals and page one, then fails; the fallback finishes.

    The fallback's first request is page two with the primary's cursor, and the primary is
    not asked again -- not even for the closing totals.
    """
    transactions = ledger(7)
    reply = history_reply(ADDRESS, newest_first(transactions), page_size=4)
    fake = KaspaFake(
        primary=ScriptedInstance(reply, reply, reply, Reply(status=503)),
        fallback=ScriptedInstance(reply),
    )

    history = await read_history(fake, max_attempts=1)

    assert history.incomplete is None
    assert len(history.effects) == 7
    assert fake.hosts_in_order == [PRIMARY_HOST] * 4 + [FALLBACK_HOST] * 3
    resumed = fake.fallback.requests[0]
    assert resumed.url.path.endswith("/full-transactions-page")
    assert resumed.url.params["before"] == str(at(4))


async def test_no_instance_answering_a_page_is_unavailable_not_incomplete() -> None:
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    fake = KaspaFake(
        primary=ScriptedInstance(reply, reply, Reply(status=503)),
        fallback=ScriptedInstance(Reply(status=503)),
    )

    with pytest.raises(ProviderUnavailableError):
        await read_history(fake, max_attempts=1)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not an address", None),
        (BIP173_TESTNET_P2WPKH, None),
        (KASPA_WRONG_NETWORK_PREFIX, None),
        (f"{ADDRESS}/../../info/health", None),
        (ADDRESS, AddressRejection.WRONG_NETWORK),
    ],
    ids=["garbage", "another chain", "a prefix it was not made for", "a path", "another network"],
)
async def test_the_address_is_validated_before_any_request(
    raw: str, reason: AddressRejection | None
) -> None:
    """Nothing is asked about a string that is not this network's address, and the refusal
    never quotes it. The last case is a testnet address against a mainnet instance."""
    fake = serving(FOUR_KINDS)
    network = "mainnet" if reason is AddressRejection.WRONG_NETWORK else "testnet"

    with pytest.raises(AddressInvalidError) as caught:
        await read_history(fake, raw, network=network)

    assert fake.requests == []
    if reason is not None:
        assert caught.value.reason is reason
    assert raw not in str(caught.value)


# --------------------------------------------------------------------------------------
# The cursor header
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    ["", "abc", "1.5", "-1", "1e12", "0&limit=1", "1" * 20, " 12"],
    ids=[
        "empty",
        "letters",
        "a fraction",
        "negative",
        "an exponent",
        "a query",
        "past int64",
        "padded",
    ],
)
async def test_a_cursor_that_is_not_plain_digits_is_refused_before_it_reaches_a_url(
    cursor: str,
) -> None:
    """The header goes back into the URL, so anything but ASCII digits is refused -- and
    the refusal does not quote it, nor is a second page asked for."""
    fake = serving(FOUR_KINDS, headers={NEXT_PAGE_HEADER: cursor})

    with pytest.raises(ProviderResponseError) as caught:
        await read_history(fake)

    assert NEXT_PAGE_HEADER in str(caught.value)
    if cursor.strip():
        assert cursor.strip() not in str(caught.value)
    assert len(fake.history_requests()) == 1


def test_the_cursor_parser_reads_digits_and_an_absent_header() -> None:
    assert parse_next_page_before(None) is None
    assert parse_next_page_before(str(at(4))) == at(4)
    assert parse_next_page_before("0") == 0


#: Digits `str.isdigit` accepts and `int` would read, which are not ASCII: full-width one
#: and two, and the superscript two a Latin-1 header byte decodes to. Built with `chr` so
#: the source holds no ambiguous character.
NON_ASCII_DIGITS: Final = (chr(0xFF11) + chr(0xFF12), chr(0xB2), "1" + chr(0x0662))


@pytest.mark.parametrize("cursor", NON_ASCII_DIGITS, ids=["full-width", "superscript", "arabic"])
def test_the_cursor_parser_refuses_digits_that_are_not_ascii(cursor: str) -> None:
    """`str.isdigit` says yes to each of these; the URL must never see one."""
    assert any(character.isdigit() for character in cursor)

    with pytest.raises(ProviderResponseError, match=NEXT_PAGE_HEADER):
        parse_next_page_before(cursor)


# --------------------------------------------------------------------------------------
# The page parser, directly
# --------------------------------------------------------------------------------------


def page_of(*documents: Any) -> str:
    return json.dumps(list(documents))


def test_the_parser_returns_accepted_rows_in_the_vendors_order() -> None:
    rejected = replace(RECEIVED, seed=9, accepted=False)

    page = parse_transactions_page(
        page_of(SPENT.document(), rejected.document(), RECEIVED.document()), ADDRESS
    )

    assert [transaction.transaction_id for transaction in page] == [
        SPENT.transaction_id,
        RECEIVED.transaction_id,
    ]
    assert [transaction.effect.delta for transaction in page] == [39_990_000 - ONE_COIN, ONE_COIN]
    assert all(transaction.resolved for transaction in page)


def test_an_unaccepted_row_is_dropped_before_any_of_its_fields_are_read() -> None:
    """Nothing on a row that is not part of the history can refuse the page."""
    document = KaspaTx(seed=9, accepted=False).document()
    document["block_time"] = "garbage"
    document["outputs"] = "garbage"

    assert parse_transactions_page(page_of(document), ADDRESS) == ()


def test_outputs_given_as_null_are_none() -> None:
    document = SPENT.document()
    document["outputs"] = None

    (transaction,) = parse_transactions_page(page_of(document), ADDRESS)

    assert transaction.effect.delta == -ONE_COIN


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (
            (datetime(2026, 10, 9, tzinfo=UTC) - EPOCH) // timedelta(milliseconds=1) - 1,
            datetime(2026, 10, 8, 23, 59, 59, 999_000, tzinfo=UTC),
        ),
        (
            (datetime(2026, 10, 9, tzinfo=UTC) - EPOCH) // timedelta(milliseconds=1),
            datetime(2026, 10, 9, tzinfo=UTC),
        ),
        (0, EPOCH),
        (LATEST_BLOCK_TIME_MS, datetime(9999, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC)),
    ],
    ids=["a millisecond before midnight", "midnight", "the epoch", "the last representable"],
)
def test_block_time_is_read_as_exact_epoch_milliseconds(
    milliseconds: int, expected: datetime
) -> None:
    """R3 takes the UTC date, so a millisecond before midnight must stay on its own day."""
    (transaction,) = parse_transactions_page(
        page_of(replace(RECEIVED, block_time=milliseconds).document()), ADDRESS
    )

    assert transaction.effect.occurred_at == expected
    assert transaction.effect.occurred_at.date() == expected.date()


def _set(path: tuple[Any, ...], value: Any) -> Any:
    def mangle(document: dict[str, Any]) -> None:
        target: Any = document
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return mangle


def _drop(path: tuple[Any, ...]) -> Any:
    def mangle(document: dict[str, Any]) -> None:
        target: Any = document
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]

    return mangle


#: Every refusal arm of `parse_transactions_page`, each from a row naming the address in an
#: output and an input, with a real id and real amounts.
REFUSALS: Final[tuple[tuple[str, Any, str], ...]] = (
    ("is_accepted absent", _drop(("is_accepted",)), "'is_accepted'"),
    ("is_accepted a string", _set(("is_accepted",), "true"), "'is_accepted'"),
    ("is_accepted null", _set(("is_accepted",), None), "'is_accepted'"),
    ("transaction_id absent", _drop(("transaction_id",)), "'transaction_id'"),
    ("transaction_id a number", _set(("transaction_id",), 7), "'transaction_id'"),
    ("block_time absent", _drop(("block_time",)), "'block_time'"),
    ("block_time a bool", _set(("block_time",), True), "'block_time'"),
    ("block_time a string", _set(("block_time",), str(at(3))), "'block_time'"),
    ("block_time fractional", _set(("block_time",), 1.5), "'block_time'"),
    ("block_time negative", _set(("block_time",), -1), "'block_time'"),
    ("block_time past 9999", _set(("block_time",), LATEST_BLOCK_TIME_MS + 1), "'block_time'"),
    ("outputs an object", _set(("outputs",), {}), "'outputs'"),
    ("outputs entry a string", _set(("outputs", 0), "output"), "outputs[]"),
    ("amount a bool", _set(("outputs", 1, "amount"), True), "outputs[].amount"),
    ("amount a string", _set(("outputs", 1, "amount"), "39990000"), "outputs[].amount"),
    ("amount fractional", _set(("outputs", 1, "amount"), 0.3999), "outputs[].amount"),
    ("amount negative", _set(("outputs", 0, "amount"), -60_000_000), "outputs[].amount"),
    ("amount absent", _drop(("outputs", 0, "amount")), "outputs[].amount"),
    ("inputs a string", _set(("inputs",), "inputs"), "'inputs'"),
    ("inputs entry a number", _set(("inputs", 0), 7), "inputs[]"),
    (
        "previous amount a bool",
        _set(("inputs", 0, "previous_outpoint_amount"), False),
        "previous_outpoint_amount",
    ),
    (
        "previous amount a string",
        _set(("inputs", 0, "previous_outpoint_amount"), "100000000"),
        "previous_outpoint_amount",
    ),
    (
        "previous amount fractional",
        _set(("inputs", 0, "previous_outpoint_amount"), 1.0),
        "previous_outpoint_amount",
    ),
    (
        "previous amount negative",
        _set(("inputs", 0, "previous_outpoint_amount"), -1),
        "previous_outpoint_amount",
    ),
)


@pytest.mark.parametrize(
    ("mangle", "names"),
    [(mangle, names) for _id, mangle, names in REFUSALS],
    ids=[name for name, _mangle, _names in REFUSALS],
)
def test_a_malformed_row_is_refused_naming_the_field_and_nothing_else(
    mangle: Any, names: str
) -> None:
    document = SPENT.document()
    mangle(document)

    with pytest.raises(ProviderResponseError) as caught:
        parse_transactions_page(page_of(document), ADDRESS)

    rendered = f"{caught.value} {caught.value.args!r}"
    assert names in rendered
    assert ADDRESS not in rendered
    assert SPENT.transaction_id not in rendered
    for amount in amounts_of((SPENT,)):
        assert amount not in rendered


@pytest.mark.parametrize(
    ("body", "names"),
    [
        (json.dumps({"transaction_id": "x"}), "dict rather than the JSON array"),
        ("<html>rate limited</html>", "not JSON"),
        (page_of("row"), "str rather than a transaction object"),
        (page_of(None), "NoneType rather than a transaction object"),
    ],
    ids=["an object", "not JSON", "a row that is a string", "a row that is null"],
)
def test_a_page_that_is_not_an_array_of_objects_is_refused(body: str, names: str) -> None:
    with pytest.raises(ProviderResponseError, match=names):
        parse_transactions_page(body, ADDRESS)


# --------------------------------------------------------------------------------------
# The count parser
# --------------------------------------------------------------------------------------


def test_the_count_parser_reads_total() -> None:
    assert parse_transaction_count(json.dumps({"total": 1_001})) == 1_001
    assert parse_transaction_count(json.dumps({"total": 0})) == 0


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({}),
        json.dumps({"total": True}),
        json.dumps({"total": -1}),
        json.dumps({"total": "12"}),
        json.dumps({"total": 1.5}),
        json.dumps([12]),
    ],
    ids=["absent", "a bool", "negative", "a string", "fractional", "an array"],
)
def test_the_count_parser_refuses_anything_but_a_whole_count(body: str) -> None:
    with pytest.raises(ProviderResponseError):
        parse_transaction_count(body)


async def test_a_balance_echoing_another_address_is_refused_mid_history() -> None:
    """The balance read is the single-address read, with its echo check: a cache answering
    about somebody else is refused, never checked a history against."""
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    wrong = replace(reply, body=balance_body(OTHER, FOUR_KINDS_BALANCE))
    fake = KaspaFake(primary=ScriptedInstance(reply, wrong))

    with pytest.raises(ProviderResponseError) as caught:
        await read_history(fake)

    assert ADDRESS not in str(caught.value)
    assert OTHER not in str(caught.value)
    assert fake.history_requests() == []


async def test_a_refusal_from_the_provider_names_no_address_id_or_amount() -> None:
    """Through the transport too: the first page carries an amount rendered as a string."""
    document = SPENT.document()
    document["outputs"][0]["amount"] = "60000000"
    reply = history_reply(ADDRESS, newest_first((SPENT,)))
    fake = KaspaFake(primary=ScriptedInstance(replace(reply, rows=(document,))))

    with pytest.raises(ProviderResponseError) as caught:
        await read_history(fake)

    rendered = f"{caught.value} {caught.value.args!r}"
    assert ADDRESS not in rendered
    assert SPENT.transaction_id not in rendered
    for amount in amounts_of((SPENT,)):
        assert amount not in rendered
