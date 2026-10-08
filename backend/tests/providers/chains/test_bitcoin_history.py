"""Spec 038, criterion 1 on Bitcoin: `EsploraProvider.address_history`.

The fake serves `/txs/chain` the way both hosts were measured to on 2026-10-08: 25 per page,
newest first, paged by the last txid of the previous page, and **an unknown cursor answers
`200 []`** -- so an empty page proves nothing, and every test that expects a complete history
also gets one only because the history proved itself (R1).

Three kinds of assertion carry the weight here, as in `test_bitcoin.py`:

* **which requests were made, to which host, with which cursor** -- a pager that read page
  one forever and a pager that read the history return the same first 25 effects;
* **the reason a history is incomplete**, never only that it is -- `moved_during_read` and
  `count_mismatch` call for different responses from whoever reads the log;
* **that no refusal names the address, a txid or an amount** -- each is the owner's holdings.

Every address is testnet or regtest (rule 3), and every txid is built at run time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
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
from portfolio.providers.chains.bitcoin import (
    BITCOIN_DECIMALS,
    HISTORY_PAGE_SIZE,
    LATEST_BLOCK_TIME,
    ChainStats,
    EsploraProvider,
    parse_chain_stats,
    parse_history_page,
)
from portfolio.providers.errors import ProviderResponseError, ProviderUnavailableError
from portfolio.providers.http import ADDRESS_BALANCE, ADDRESS_HISTORY, ENDPOINT_EXTENSION
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    CORE_REGTEST_P2WPKH,
    KASPA_TESTNET_V0,
)
from tests.providers.chains.harness import (
    BLOCK_TIME,
    FALLBACK_HOST,
    PRIMARY_HOST,
    EsploraFake,
    EsploraTx,
    Reply,
    ScriptedInstance,
    balance_body,
    esplora_provider,
    esplora_settings,
    history_address,
    history_cursor,
    history_reply,
    txid_of,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ADDRESS: Final = BIP173_TESTNET_P2WPKH
"""The address whose history is read."""
OTHER: Final = BIP173_TESTNET_P2WSH
THIRD: Final = BIP350_TESTNET_V1

ONE_COIN: Final = 100_000_000
DUST: Final = 54_321
SUBSIDY: Final = 5_000_000_000


def at(seed: int) -> int:
    """A block time ten minutes per seed after `BLOCK_TIME`: newer seeds are later blocks."""
    return BLOCK_TIME + seed * 600


def when(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


# One of each kind of effect, oldest first. Every amount is distinct, so a delta that was
# computed from the wrong field, or the wrong side, cannot coincide with the right one.
COINBASE: Final = EsploraTx(
    seed=1, block_time=at(1), coinbase=True, outputs=((ADDRESS, SUBSIDY), (None, 0))
)
RECEIVED: Final = EsploraTx(
    seed=2,
    block_time=at(2),
    inputs=((OTHER, 150_000_000),),
    outputs=((ADDRESS, ONE_COIN), (OTHER, 49_990_000)),
)
SPENT: Final = EsploraTx(
    seed=3,
    block_time=at(3),
    inputs=((ADDRESS, ONE_COIN),),
    outputs=((THIRD, 60_000_000), (ADDRESS, 39_990_000)),
)
SELF_TRANSFER: Final = EsploraTx(
    seed=4,
    block_time=at(4),
    inputs=((ADDRESS, 39_990_000), (OTHER, 7_000)),
    outputs=((ADDRESS, 39_990_000),),
)
"""Spends from the address and pays the same amount back, with another input paying the fee:
it nets to exactly zero, and an effect computed from one side alone would not."""

FOUR_KINDS: Final = (COINBASE, RECEIVED, SPENT, SELF_TRANSFER)
FOUR_KINDS_BALANCE: Final = SUBSIDY + ONE_COIN - 60_010_000


def newest_first(transactions: Sequence[EsploraTx]) -> list[EsploraTx]:
    return list(reversed(transactions))


def ledger(count: int) -> list[EsploraTx]:
    """`count` transactions oldest first: a coinbase, then receipts and spends alternating.

    Each spend is smaller than what has arrived before it, so the balance never goes below
    zero at any point -- a realistic history, though the provider would sum any other.
    """
    transactions: list[EsploraTx] = []
    for seed in range(1, count + 1):
        if seed == 1:
            transactions.append(
                EsploraTx(
                    seed=seed, block_time=at(seed), coinbase=True, outputs=((ADDRESS, SUBSIDY),)
                )
            )
        elif seed % 2 == 0:
            transactions.append(
                EsploraTx(
                    seed=seed,
                    block_time=at(seed),
                    inputs=((OTHER, DUST + seed + 1_000),),
                    outputs=((ADDRESS, DUST + seed),),
                )
            )
        else:
            transactions.append(
                EsploraTx(
                    seed=seed,
                    block_time=at(seed),
                    inputs=((ADDRESS, seed * 1_000 + 500),),
                    outputs=((OTHER, seed * 1_000), (ADDRESS, 300)),
                )
            )
    return transactions


async def read_history(fake: EsploraFake, address: str = ADDRESS, **options: Any) -> AddressHistory:
    provider, client = esplora_provider(fake, **options)
    async with client:
        return await provider.address_history(address)


def serving(transactions: Sequence[EsploraTx], **overrides: Any) -> EsploraFake:
    """One instance serving `transactions` (oldest first) and stats that agree with them."""
    reply = history_reply(ADDRESS, newest_first(transactions), **overrides)
    return EsploraFake(primary=ScriptedInstance(reply))


def amounts_of(transactions: Sequence[EsploraTx]) -> set[str]:
    """Every amount in a set of transactions, as the digits a message could leak."""
    values = {
        value for transaction in transactions for _owner, value in transaction.outputs if value
    }
    values |= {spend[1] for transaction in transactions for spend in transaction.inputs if spend}
    return {str(value) for value in values if value >= 1_000}


# --------------------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------------------


_HISTORY: TransactionHistoryReader = EsploraProvider(
    httpx.AsyncClient(), settings=esplora_settings()
)
"""`mypy --strict` deciding that the Esplora provider satisfies the protocol's signature.

The client is never used, so importing this module opens no connection pool.
"""


def test_the_esplora_provider_is_a_transaction_history_reader() -> None:
    """The run-time gate the rebuild uses; the signature is the assignment above's."""
    assert isinstance(_HISTORY, TransactionHistoryReader)


# --------------------------------------------------------------------------------------
# A complete history
# --------------------------------------------------------------------------------------


async def test_each_kind_of_transaction_has_its_net_effect_oldest_first() -> None:
    """R2, row by row: received, spent, a self-transfer netting to zero, and a coinbase.

    The coinbase's input has no `prevout` and spends nothing; its `OP_RETURN` output has no
    address and pays nobody. The effects come back oldest first, each at its block's time,
    aware and in UTC.
    """
    fake = serving(FOUR_KINDS)

    history = await read_history(fake)

    assert history == AddressHistory(
        address=ADDRESS,
        balance=FOUR_KINDS_BALANCE,
        decimals=BITCOIN_DECIMALS,
        effects=(
            TxEffect(occurred_at=when(at(1)), delta=SUBSIDY),
            TxEffect(occurred_at=when(at(2)), delta=ONE_COIN),
            TxEffect(occurred_at=when(at(3)), delta=39_990_000 - ONE_COIN),
            TxEffect(occurred_at=when(at(4)), delta=0),
        ),
        incomplete=None,
    )
    assert all(effect.occurred_at.tzinfo is UTC for effect in history.effects)


async def test_the_reads_are_stats_then_pages_by_txid_cursor_then_stats_again() -> None:
    """The documented order, the labels, and the path form of the cursor.

    The stats reads are `address_balance` -- the same request as a balance read -- and the
    pages are `address_history`. No request carries a query: `?after_txid=` is the form
    both hosts ignore on `/txs/chain`, and a pager using it would read page one forever.
    """
    fake = serving(FOUR_KINDS)

    await read_history(fake)

    paths = [request.url.path for request in fake.requests]
    assert paths == [
        f"/api/address/{ADDRESS}",
        f"/api/address/{ADDRESS}/txs/chain",
        f"/api/address/{ADDRESS}/txs/chain/{COINBASE.txid}",
        f"/api/address/{ADDRESS}",
    ]
    labels = [request.extensions.get(ENDPOINT_EXTENSION) for request in fake.requests]
    assert labels == [ADDRESS_BALANCE, ADDRESS_HISTORY, ADDRESS_HISTORY, ADDRESS_BALANCE]
    assert all(request.url.query == b"" for request in fake.requests)
    assert all(request.method == "GET" for request in fake.requests)


async def test_a_history_over_several_pages_is_read_to_the_end_and_proves_itself() -> None:
    """53 transactions: 25, 25, 3, then the empty page -- four pages, the cap exactly.

    Each cursor is the last txid of the page before it. The effects are all 53, oldest
    first, and they sum to the balance the stats reported.
    """
    transactions = ledger(53)
    fake = serving(transactions)

    history = await read_history(fake)

    assert history.incomplete is None
    assert [effect.occurred_at for effect in history.effects] == [
        when(transaction.block_time) for transaction in transactions
    ]
    assert sum(effect.delta for effect in history.effects) == history.balance
    assert history.balance == sum(
        transaction.funded(ADDRESS) - transaction.spent(ADDRESS) for transaction in transactions
    )
    cursors = [history_cursor(request) for request in fake.history_requests()]
    assert cursors == [None, txid_of(29), txid_of(4), txid_of(1)]
    assert all(history_address(request) == ADDRESS for request in fake.history_requests())


@pytest.mark.parametrize("count", [0, 1, 24, 25, 26, 50, 51])
async def test_every_history_length_costs_one_page_more_than_its_full_pages(count: int) -> None:
    """⌈N/25⌉ pages of transactions and the empty one, within the cap at every boundary.

    The boundaries are where a cap of `tx_count // 25 + 2` could be one too tight: a history
    that is an exact multiple of 25 still needs its empty page.
    """
    fake = serving(ledger(count))

    history = await read_history(fake)

    assert history.incomplete is None
    assert len(history.effects) == count
    full_pages = -(-count // HISTORY_PAGE_SIZE)
    assert len(fake.history_requests()) == full_pages + 1


async def test_an_address_with_no_history_is_complete_and_empty() -> None:
    """Zero transactions, a zero balance, and one empty page: complete, not missing."""
    fake = serving(())

    history = await read_history(fake)

    assert history == AddressHistory(
        address=ADDRESS, balance=0, decimals=BITCOIN_DECIMALS, effects=(), incomplete=None
    )
    assert len(fake.history_requests()) == 1


async def test_the_address_is_read_in_its_canonical_form() -> None:
    """An upper-case bech32 address is the same address; the URL and the result carry the
    lower-case form the vendor echoes and the outputs name."""
    fake = serving(FOUR_KINDS)

    history = await read_history(fake, BIP173_TESTNET_P2WPKH_UPPERCASE)

    assert history.address == ADDRESS
    assert history.incomplete is None
    assert all(ADDRESS in request.url.path for request in fake.requests)


async def test_a_transaction_seen_on_two_pages_is_counted_once() -> None:
    """A txid repeated across a page boundary is one transaction, and the count decides.

    The vendor counts 30 distinct transactions; the pages carry one of them twice. Kept
    once, the 30 prove themselves.
    """
    transactions = ledger(30)
    served = newest_first(transactions)
    repeated = [*served[:25], served[24], *served[25:]]
    reply = history_reply(ADDRESS, repeated)
    fake = EsploraFake(primary=ScriptedInstance(reply))

    history = await read_history(fake)

    assert reply.tx_count == 30
    assert history.incomplete is None
    assert len(history.effects) == 30


async def test_a_repeated_transaction_the_vendor_also_counts_is_a_count_mismatch() -> None:
    """The control: when the count includes the repeat, 30 distinct do not make 31."""
    transactions = ledger(30)
    served = newest_first(transactions)
    repeated = [*served[:25], served[24], *served[25:]]
    fake = EsploraFake(primary=ScriptedInstance(history_reply(ADDRESS, repeated, tx_count=31)))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH


# --------------------------------------------------------------------------------------
# An incomplete history, and why
# --------------------------------------------------------------------------------------


async def test_fewer_transactions_than_the_vendor_counts_is_a_count_mismatch() -> None:
    """The history ended -- an empty page -- one transaction short of `tx_count`.

    This is what a reorged-out cursor looks like: `200 []` where a page should have been.
    The effects are what was collected, and nothing may store them.
    """
    fake = serving(FOUR_KINDS, tx_count=5)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH
    assert len(history.effects) == 4


async def test_a_cursor_answered_with_an_empty_page_too_early_is_a_count_mismatch() -> None:
    """Page one, then an empty page, from a vendor that counts 30: the end is not proven."""
    transactions = ledger(30)
    full = history_reply(ADDRESS, newest_first(transactions))
    truncated = replace(full, history=full.history[:HISTORY_PAGE_SIZE])
    fake = EsploraFake(primary=ScriptedInstance(full, truncated))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH
    assert len(history.effects) == HISTORY_PAGE_SIZE


async def test_effects_that_do_not_sum_to_the_balance_are_a_balance_mismatch() -> None:
    """Every transaction counted, and the sums still disagree by one dust amount."""
    fake = serving(FOUR_KINDS, funded=SUBSIDY + ONE_COIN + 39_990_000 * 2 + DUST)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.BALANCE_MISMATCH
    assert history.balance == FOUR_KINDS_BALANCE + DUST


@pytest.mark.parametrize(
    "moved",
    [
        {"tx_count": 5},
        {"funded": SUBSIDY + ONE_COIN + 39_990_000 * 2 + DUST},
        {"spent": ONE_COIN + 39_990_000 + DUST},
    ],
    ids=["a new transaction", "more funded", "more spent"],
)
async def test_stats_that_changed_during_the_read_mean_it_moved(moved: dict[str, Any]) -> None:
    """The stats after the paging differ from the stats before it in any one of the three.

    A transaction that confirmed mid-read, or a reorg: either way the history read is of
    neither moment. The balance reported is the one read before, which the effects were
    collected against.
    """
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    fake = EsploraFake(primary=ScriptedInstance(reply, reply, reply, replace(reply, **moved)))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.MOVED_DURING_READ
    assert history.balance == FOUR_KINDS_BALANCE
    assert len(fake.requests) == 4


async def test_an_input_with_no_prevout_is_an_unresolved_input() -> None:
    """A non-coinbase input without `prevout`: its source is unknown, so the history is.

    The stats agree with the transactions as the vendor counts them -- so the only thing
    wrong is the input, and the reason names it rather than the sum it would have upset.
    """
    unresolved = EsploraTx(
        seed=5,
        block_time=at(5),
        inputs=(None,),
        outputs=((OTHER, DUST),),
    )
    fake = serving((*FOUR_KINDS, unresolved))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.UNRESOLVED_INPUT
    assert history.effects[-1] == TxEffect(occurred_at=when(at(5)), delta=0)


async def test_a_vendor_that_never_ends_the_history_stops_at_the_cap() -> None:
    """Page one for every cursor: the pager stops at `tx_count // 25 + 2` pages.

    Kept once, the three transactions even number `tx_count` and sum to the balance -- so
    it is the missing end, and only that, that makes this a count mismatch. Without the
    cap this test would not finish.
    """
    fake = serving(FOUR_KINDS[:3], ignore_cursor=True)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH
    assert len(history.effects) == 3
    assert sum(effect.delta for effect in history.effects) == history.balance
    assert len(fake.history_requests()) == 3 // HISTORY_PAGE_SIZE + 2


async def test_moved_during_the_read_outranks_every_other_reason() -> None:
    """A transaction that confirmed mid-read explains a count and a sum that disagree, so it
    is the reason given -- not the symptoms it caused."""
    unresolved = EsploraTx(seed=5, block_time=at(5), inputs=(None,), outputs=((OTHER, DUST),))
    reply = history_reply(ADDRESS, newest_first((*FOUR_KINDS, unresolved)), tx_count=9)
    fake = EsploraFake(primary=ScriptedInstance(reply, reply, reply, replace(reply, tx_count=10)))

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.MOVED_DURING_READ


async def test_an_unresolved_input_outranks_a_count_and_a_sum_that_disagree() -> None:
    """An input of unknown source makes the sum unknowable, so it is named first."""
    unresolved = EsploraTx(seed=5, block_time=at(5), inputs=(None,), outputs=((OTHER, DUST),))
    fake = serving((*FOUR_KINDS, unresolved), tx_count=9, funded=2 * SUBSIDY)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.UNRESOLVED_INPUT


async def test_a_count_mismatch_outranks_a_sum_that_disagrees() -> None:
    """A history that ended short cannot be expected to sum to the balance."""
    fake = serving(FOUR_KINDS, tx_count=9, funded=2 * SUBSIDY)

    history = await read_history(fake)

    assert history.incomplete is HistoryIncomplete.COUNT_MISMATCH


# --------------------------------------------------------------------------------------
# Failover and validation
# --------------------------------------------------------------------------------------


async def test_failover_mid_history_is_sticky_and_carries_the_cursor() -> None:
    """The primary answers the stats and page one, then fails; the fallback finishes.

    The fallback's first request is page two, with the cursor page one ended on -- not page
    one again, which would read 25 transactions twice -- and the primary is never asked
    again, not even for the closing stats read.
    """
    transactions = ledger(30)
    reply = history_reply(ADDRESS, newest_first(transactions))
    fake = EsploraFake(
        primary=ScriptedInstance(reply, reply, Reply(status=503)),
        fallback=ScriptedInstance(reply),
    )

    history = await read_history(fake, max_attempts=1)

    assert history.incomplete is None
    assert len(history.effects) == 30
    assert fake.hosts_in_order == [
        PRIMARY_HOST,
        PRIMARY_HOST,
        PRIMARY_HOST,
        FALLBACK_HOST,
        FALLBACK_HOST,
        FALLBACK_HOST,
    ]
    assert history_cursor(fake.fallback.requests[0]) == txid_of(6)
    assert fake.fallback.requests[-1].url.path == f"/api/address/{ADDRESS}"


async def test_no_instance_answering_a_page_is_unavailable_not_incomplete() -> None:
    """A history that could not be read is an error, never a result to be judged."""
    reply = history_reply(ADDRESS, newest_first(FOUR_KINDS))
    fake = EsploraFake(
        primary=ScriptedInstance(reply, Reply(status=503)),
        fallback=ScriptedInstance(Reply(status=503)),
    )

    with pytest.raises(ProviderUnavailableError):
        await read_history(fake, max_attempts=1)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not an address", None),
        (KASPA_TESTNET_V0, None),
        (CORE_REGTEST_P2WPKH, AddressRejection.WRONG_NETWORK),
        (f"{ADDRESS}/../../blocks/tip/height", None),
    ],
    ids=["garbage", "another chain", "another network", "a path"],
)
async def test_the_address_is_validated_before_any_request(
    raw: str, reason: AddressRejection | None
) -> None:
    """Nothing is asked of any instance about a string that is not this network's address.

    The address goes into a path; validating it first is what keeps a database value from
    becoming `GET /address/../../blocks/tip/height`. The refusal never quotes the string.
    """
    fake = serving(FOUR_KINDS)

    with pytest.raises(AddressInvalidError) as caught:
        await read_history(fake, raw)

    assert fake.requests == []
    if reason is not None:
        assert caught.value.reason is reason
    assert raw not in str(caught.value)


# --------------------------------------------------------------------------------------
# The page parser, directly
# --------------------------------------------------------------------------------------


def page_of(*documents: Any) -> str:
    return json.dumps(list(documents))


def mangled(transaction: EsploraTx, mangle: Any) -> str:
    """One transaction's page, after `mangle(document)` broke one field of it."""
    document = transaction.document()
    mangle(document)
    return page_of(document)


def test_the_parser_returns_each_transaction_in_the_vendors_order() -> None:
    page = parse_history_page(page_of(SPENT.document(), RECEIVED.document()), ADDRESS)

    assert [transaction.txid for transaction in page] == [SPENT.txid, RECEIVED.txid]
    assert [transaction.effect.delta for transaction in page] == [39_990_000 - ONE_COIN, ONE_COIN]
    assert all(transaction.resolved for transaction in page)


def test_a_block_time_at_the_last_representable_second_is_read() -> None:
    """The bound is inclusive: 9999-12-31T23:59:59Z is a time, one second later is not."""
    late = replace(RECEIVED, block_time=LATEST_BLOCK_TIME)

    (transaction,) = parse_history_page(page_of(late.document()), ADDRESS)

    assert transaction.effect.occurred_at == datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _set(path: tuple[Any, ...], value: Any) -> Any:
    """A mangler setting the field at `path` -- keys and list indices -- to `value`."""

    def mangle(document: dict[str, Any]) -> None:
        target: Any = document
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return mangle


def _drop(path: tuple[Any, ...]) -> Any:
    """A mangler removing the field at `path`."""

    def mangle(document: dict[str, Any]) -> None:
        target: Any = document
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]

    return mangle


#: Every refusal arm of `parse_history_page`, each driven from a transaction that names the
#: address in an output and an input, with a real txid and real amounts -- so a message that
#: quoted any of them would be caught by the assertions below.
REFUSALS: Final[tuple[tuple[str, Any, str], ...]] = (
    ("txid absent", _drop(("txid",)), "'txid'"),
    ("txid upper-case", _set(("txid",), SPENT.txid.upper()), "'txid'"),
    ("txid short", _set(("txid",), SPENT.txid[:63]), "'txid'"),
    ("txid a path", _set(("txid",), "../../blocks/tip/height"), "'txid'"),
    ("txid trailing newline", _set(("txid",), f"{SPENT.txid}\n"), "'txid'"),
    ("status absent", _drop(("status",)), "'status'"),
    ("status a list", _set(("status",), []), "'status'"),
    ("unconfirmed", _set(("status", "confirmed"), False), "not confirmed"),
    ("confirmed as a string", _set(("status", "confirmed"), "true"), "not confirmed"),
    ("block_time absent", _drop(("status", "block_time")), "block_time"),
    ("block_time a bool", _set(("status", "block_time"), True), "block_time"),
    ("block_time a string", _set(("status", "block_time"), str(at(3))), "block_time"),
    ("block_time fractional", _set(("status", "block_time"), 1.5), "block_time"),
    ("block_time negative", _set(("status", "block_time"), -1), "block_time"),
    ("block_time past 9999", _set(("status", "block_time"), LATEST_BLOCK_TIME + 1), "block_time"),
    ("vout absent", _drop(("vout",)), "'vout'"),
    ("vout an object", _set(("vout",), {}), "'vout'"),
    ("vout entry a string", _set(("vout", 0), "output"), "vout[]"),
    ("vout value a bool", _set(("vout", 1, "value"), True), "vout[].value"),
    ("vout value a string", _set(("vout", 1, "value"), "39990000"), "vout[].value"),
    ("vout value fractional", _set(("vout", 1, "value"), 0.3999), "vout[].value"),
    ("vout value negative", _set(("vout", 0, "value"), -60_000_000), "vout[].value"),
    ("vout value absent", _drop(("vout", 0, "value")), "vout[].value"),
    ("vin a string", _set(("vin",), "inputs"), "'vin'"),
    ("vin entry a number", _set(("vin", 0), 7), "vin[]"),
    ("prevout a string", _set(("vin", 0, "prevout"), "resolved"), "vin[].prevout"),
    ("prevout value a bool", _set(("vin", 0, "prevout", "value"), False), "prevout.value"),
    ("prevout value a string", _set(("vin", 0, "prevout", "value"), "1"), "prevout.value"),
    ("prevout value fractional", _set(("vin", 0, "prevout", "value"), 1.0), "prevout.value"),
    ("prevout value negative", _set(("vin", 0, "prevout", "value"), -1), "prevout.value"),
)


@pytest.mark.parametrize(
    ("mangle", "names"),
    [(mangle, names) for _id, mangle, names in REFUSALS],
    ids=[name for name, _mangle, _names in REFUSALS],
)
def test_a_malformed_transaction_is_refused_naming_the_field_and_nothing_else(
    mangle: Any, names: str
) -> None:
    """Each arm refuses with a message naming the field -- and never the address, the txid,
    or any amount the body carried."""
    with pytest.raises(ProviderResponseError) as caught:
        parse_history_page(mangled(SPENT, mangle), ADDRESS)

    rendered = f"{caught.value} {caught.value.args!r}"
    assert names in rendered
    assert ADDRESS not in rendered
    assert SPENT.txid not in rendered
    assert SPENT.txid.upper() not in rendered
    for amount in amounts_of((SPENT,)):
        assert amount not in rendered


@pytest.mark.parametrize(
    ("body", "names"),
    [
        (json.dumps({"txid": txid_of(1)}), "dict rather than the JSON array"),
        ("<html>rate limited</html>", "not JSON"),
        (page_of(txid_of(1)), "str rather than a transaction object"),
        (page_of(None), "NoneType rather than a transaction object"),
    ],
    ids=["an object", "not JSON", "an entry that is a string", "an entry that is null"],
)
def test_a_page_that_is_not_an_array_of_objects_is_refused(body: str, names: str) -> None:
    with pytest.raises(ProviderResponseError, match=names) as caught:
        parse_history_page(body, ADDRESS)

    assert txid_of(1) not in str(caught.value)


def test_an_output_without_an_address_pays_nobody_but_its_value_is_still_checked() -> None:
    """An `OP_RETURN` output has no `scriptpubkey_address`: not the address's, and still an
    amount that has to be a whole number of satoshis."""
    (transaction,) = parse_history_page(page_of(COINBASE.document()), ADDRESS)
    assert transaction.effect.delta == SUBSIDY

    with pytest.raises(ProviderResponseError, match=r"vout\[\]\.value"):
        parse_history_page(mangled(COINBASE, _set(("vout", 1, "value"), True)), ADDRESS)


def test_a_coinbase_input_is_skipped_even_with_a_prevout() -> None:
    """`is_coinbase: true` spends nothing, whatever else the entry carries."""
    document = COINBASE.document()
    document["vin"][0]["prevout"] = {"scriptpubkey_address": ADDRESS, "value": "not read"}

    (transaction,) = parse_history_page(page_of(document), ADDRESS)

    assert transaction.effect.delta == SUBSIDY
    assert transaction.resolved is True


# --------------------------------------------------------------------------------------
# The stats parser
# --------------------------------------------------------------------------------------


def test_the_stats_parser_reads_the_count_and_both_sums() -> None:
    body = balance_body(ADDRESS, funded=3 * ONE_COIN, spent=ONE_COIN + DUST, tx_count=7)

    stats = parse_chain_stats(body, ADDRESS)

    assert stats == ChainStats(tx_count=7, funded=3 * ONE_COIN, spent=ONE_COIN + DUST)
    assert stats.balance == 2 * ONE_COIN - DUST


@pytest.mark.parametrize(
    ("body", "names"),
    [
        (balance_body(OTHER, funded=ONE_COIN), "'address'"),
        (balance_body(ADDRESS, funded=DUST, spent=ONE_COIN), "negative"),
        (balance_body(ADDRESS, funded=ONE_COIN, tx_count=-1), "chain_stats.tx_count"),
        (json.dumps({"address": ADDRESS}), "'chain_stats'"),
    ],
    ids=["another address", "spent over funded", "a negative count", "no chain_stats"],
)
def test_the_stats_parser_refuses_what_the_balance_parser_refuses(body: str, names: str) -> None:
    """One parser for the confirmed half of the body, so the two reads refuse identically."""
    with pytest.raises(ProviderResponseError) as caught:
        parse_chain_stats(body, ADDRESS)

    assert names in str(caught.value)
    assert ADDRESS not in str(caught.value)
    assert str(ONE_COIN) not in str(caught.value)


async def test_a_refusal_from_the_provider_names_no_address_txid_or_amount() -> None:
    """Through the transport too: the first page carries an unconfirmed transaction."""
    document = SPENT.document()
    document["status"] = {"confirmed": False}
    reply = history_reply(ADDRESS, newest_first((SPENT,)))
    fake = EsploraFake(primary=ScriptedInstance(replace(reply, history=(document,))))

    with pytest.raises(ProviderResponseError) as caught:
        await read_history(fake)

    rendered = f"{caught.value} {caught.value.args!r}"
    assert ADDRESS not in rendered
    assert SPENT.txid not in rendered
    for amount in amounts_of((SPENT,)):
        assert amount not in rendered
