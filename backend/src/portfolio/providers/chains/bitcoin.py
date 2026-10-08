"""Bitcoin balances from an Esplora instance, with a second instance behind the first.

The first provider to travel through the seam #6 built. It reads
`GET /address/:address`, derives the confirmed balance from `chain_stats` and the pending
delta from `mempool_stats`, and answers `GET /blocks/tip/height` for health.

## What was confirmed against the vendors' documentation, and when

Read on **2026-09-22**, and separated from what was assumed because the next person cannot
tell the difference otherwise and will trust both equally.

**Confirmed**, from Blockstream's published `API.md` and mempool.space's REST
documentation:

* `GET /address/:address` returns `address`, `chain_stats` and `mempool_stats`, each stat
  object carrying `tx_count`, `funded_txo_count`, `funded_txo_sum`, `spent_txo_count` and
  `spent_txo_sum`. The sums are in **satoshis**.
* `GET /blocks/tip/height` returns the height of the last block, as a plain integer body.
* The public base URLs are `https://blockstream.info/api` (with `/testnet/api` and
  `/signet/api` for the other networks) and `https://mempool.space/api` (with
  `https://mempool.space/testnet/api`).
* mempool.space states that exceeding its limits returns HTTP 429 and that repeatedly
  exceeding them may result in a ban. It publishes **no numbers**. Blockstream documents
  no rate limit at all.

**Assumed, because neither vendor documents it:**

* **What either instance answers for an address it considers invalid.** Neither documents
  an error body or even a status for that case, which is why every mapping below is
  written against the *status code* alone -- the part both vendors do have to get right --
  and why a wrong-network address is refused here, offline, rather than by asking.
* That `mempool_stats` is always present in practice. Its absence is read as "this
  instance cannot tell you", not as a zero.

`docs/providers.md` carries the same split, and the date, for a reader who never opens
this file.

## The parser is hand-written, and that is a disclosure decision rather than a taste

A pydantic model would be shorter. Its `ValidationError` **renders the input that
failed**, and the input here is a response body containing the owner's address -- which
then travels into a log the moment anything calls `logger.exception`. That is #44 arriving
through a different door, and the same defect #5 found in `services/wallets.py`.

So every refusal below is raised by hand, and every message names **a field and a type**
and never a value. No rejection in this module contains an address or any part of a body.

## Two instances, and where the failover rule now lives

**`providers/endpoints.py` owns it**, and this module owns nothing of it but the two
settings it reads and the vendor name that appears in an exhaustion message. #7 wrote the
loop here and review corrected it here; #8 added a second provider that needs the same
rule, and a rule corrected once in review must not exist twice. Read `endpoints.py` for the
argument in full. The three sentences that matter to a reader of this file:

* **Every failure to answer moves to the next instance** -- a transport error, a 5xx, a
  429, a 403, a 401, a 404, a 3xx. The rule it replaced stopped on any 4xx, which sounds
  right and made the fallback unreachable in exactly the cases a fallback exists for: a ban
  mempool.space does not document the status of, a self-hosted Esplora behind an auth proxy
  returning 401, a base URL that forgot its `/api` returning 404 forever.
* **A 200 whose body will not parse still stops the call**, and that asymmetry is the
  point. A non-200 is one instance declining to answer; a 200 we cannot read is a statement
  about our parser or the vendor's schema, and a second opinion would either repeat it or
  hide it behind a number. That decision is here, in `fetch_balances`, because only this
  module knows what a body means.
* **Failover is sticky within one `fetch_balances` call and resets between calls.** Reading
  twenty addresses against an instance that just refused the first is how a soft throttle
  becomes the ban mempool.space warns about; an instance throttled five minutes ago is the
  one we would rather be using now.

The reads are sequential. `max_addresses_per_call` is 1, so twenty addresses is twenty
calls spaced by `HostRateLimiter`; a `gather` would hand the limiter twenty simultaneous
acquisitions and turn a floor into a queue whose depth nobody bounded.

## Extended public keys (spec 031)

`scan_extended_key` derives a key's addresses locally (`domain/extended_keys.py`) and reads
each one through the same per-address request `fetch_balances` makes, because neither
vendor serves a lookup by extended key. Whether an address is used comes from `tx_count`,
which both vendors document in both stats objects (re-read on **2026-10-03**; see
`docs/providers.md`, *Extended public keys*). The gap limit, the cap and the order of work
are in the method's docstring.

## Transaction history (spec 038)

`address_history` reads an address's whole confirmed history, for the rebuild of its past
balances. Confirmed against both vendors' documentation and measured on both hosts on
**2026-10-08** (`docs/providers.md`, *Transaction history, for the balance rebuild*):

* `GET /address/:a/txs/chain[/:last_seen_txid]` "Returns 25 transactions per page", newest
  first. Paging by the last txid of the previous page reaches the oldest transaction on both
  hosts. **The path form is the only one both honour**: `?after_txid=` is ignored on
  `/txs/chain` by both, so a pager relying on it would read page one forever.
* **An unknown or reorged-out cursor answers `200 []`**, exactly like the end of the history.
  An empty page therefore proves nothing on its own, and the history proves itself instead
  (R1): the distinct txids collected equal `chain_stats.tx_count`, their effects sum to
  `funded_txo_sum - spent_txo_sum`, and the stats read before the paging equal those read
  after it. Anything else is returned as incomplete, with the reason, and never as a zero.
* Amounts are integer satoshis; `status.block_time` is Unix seconds and is absent, not
  `null`, on an unconfirmed transaction; a coinbase input carries no `prevout`.

**Assumed**: the order of transactions within one block (undocumented; nothing here depends
on it), and that a page shorter than 25 is the last -- not relied on: the pager reads on to
the empty page. Block header times are not monotonic in height, so `occurred_at` can step
backwards by a little between two effects in chain order.

## Nothing here logs

Not one call. The shared transport logs `"{scheme}://{host}/{label}"` and nothing else,
which is the only log contract in this package that is enforced rather than remembered. A
log line written here would bypass all of it, and both vendors put the address in the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import httpx

from portfolio.config import get_settings
from portfolio.domain.addresses import (
    AddressInvalidError,
    AddressRejection,
    BitcoinNetwork,
    bitcoin_network_of,
)
from portfolio.domain.chains import ChainKey
from portfolio.domain.chains import validate_address as validate_chain_address
from portfolio.domain.extended_keys import (
    CHANGE_BRANCH,
    HARDENED_INDEX,
    MAX_ADDRESSES_PER_BRANCH,
    NETWORK_FAMILY_BY_NETWORK,
    RECEIVE_BRANCH,
    ScriptType,
    address_of,
    addresses_to_extend,
    derive_child,
    parse_extended_public_key,
)
from portfolio.providers.base import (
    AddressHistory,
    ChainCapabilities,
    ExtendedKeyScan,
    HistoryIncomplete,
    ProviderHealth,
    ScannedAddress,
    TxEffect,
    align_balances,
    decode_json,
    require_json_object,
)
from portfolio.providers.endpoints import FALLBACK, PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    ADDRESS_HISTORY,
    BLOCK_TIP_HEIGHT,
    ENDPOINT_EXTENSION,
)
from portfolio.providers.registry import register_chain_provider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.config import Settings
    from portfolio.domain.chains import ValidatedAddress
    from portfolio.domain.extended_keys import DerivedKey, ExtendedPublicKey
    from portfolio.providers.base import AddressBalance, KnownDerivedAddress
    from portfolio.providers.endpoints import Endpoint

__all__ = [
    "ADDRESS_PATH",
    "BITCOIN_DECIMALS",
    "BRANCH_CAP_MESSAGE",
    "CAPABILITIES",
    "FALLBACK",
    "HISTORY_PAGE_PATH",
    "HISTORY_PAGE_SIZE",
    "HISTORY_PATH",
    "LATEST_BLOCK_TIME",
    "PRIMARY",
    "TIP_HEIGHT_PATH",
    "VENDOR",
    "AddressStats",
    "ChainStats",
    "ChainTransaction",
    "EsploraProvider",
    "parse_address_response",
    "parse_chain_stats",
    "parse_history_page",
    "parse_tip_height",
]

BITCOIN_DECIMALS: Final = 8
"""Satoshis to bitcoin. Carried on every balance as well as on the capabilities, so a
stored reading stays interpretable without asking which provider produced it."""

MAX_ADDRESSES_PER_CALL: Final = 1
"""Esplora documents a single-address balance endpoint and no batch endpoint at all."""

ADDRESS_PATH: Final = "/address/{address}"
TIP_HEIGHT_PATH: Final = "/blocks/tip/height"
HISTORY_PATH: Final = "/address/{address}/txs/chain"
HISTORY_PAGE_PATH: Final = "/address/{address}/txs/chain/{txid}"
"""The first page of an address's confirmed history, and every later one by the path form.

The path form is the only paging both hosts honour (2026-10-08): `?after_txid=` is ignored on
`/txs/chain` by both, and a pager that relied on it would be served page one forever."""

HISTORY_PAGE_SIZE: Final = 25
""""Returns 25 transactions per page" -- both vendors' documentation, and measured on both.

Used for one thing: the page cap, `tx_count // HISTORY_PAGE_SIZE + 2`, which is one more page
than a history of `tx_count` transactions needs (every full page, a remainder, and the empty
page that ends it). A vendor that keeps answering past it is not ending the history, and the
read stops there rather than following it without end."""

LATEST_BLOCK_TIME: Final = 253_402_300_799
"""The last Unix second a `datetime` can hold, 9999-12-31T23:59:59Z.

A `block_time` past it is refused as a response error rather than left to raise an
`OverflowError` or a `ValueError` out of `datetime.fromtimestamp` -- which one depends on the
platform's `time_t`, and neither is in this package's vocabulary."""

VENDOR: Final = "Esplora"
"""What this provider's upstream is called in an exhaustion message.

The software's name, never a host. It is rendered into a `ProviderError`, which reaches a
log and a traceback, and naming the deployment there is the disclosure `request_target`
exists to prevent.

`PRIMARY` and `FALLBACK` are re-exported from `providers/endpoints.py` rather than defined
here, since every provider with a fallback calls its positions the same two things.
"""

# The field names, written down once. A typo in one of these is a parser that refuses
# every well-formed response, which is a failure mode worth making greppable.
ADDRESS_FIELD: Final = "address"
CHAIN_STATS: Final = "chain_stats"
MEMPOOL_STATS: Final = "mempool_stats"
FUNDED_SUM: Final = "funded_txo_sum"
SPENT_SUM: Final = "spent_txo_sum"
TX_COUNT: Final = "tx_count"
TXID_FIELD: Final = "txid"
STATUS_FIELD: Final = "status"
CONFIRMED_FIELD: Final = "confirmed"
BLOCK_TIME_FIELD: Final = "block_time"
VIN_FIELD: Final = "vin"
VOUT_FIELD: Final = "vout"
PREVOUT_FIELD: Final = "prevout"
IS_COINBASE_FIELD: Final = "is_coinbase"
SCRIPTPUBKEY_ADDRESS_FIELD: Final = "scriptpubkey_address"
VALUE_FIELD: Final = "value"

_TXID: Final = re.compile(r"[0-9a-f]{64}")
"""A txid as Esplora renders one: 64 lower-case hexadecimal characters.

**Checked because a txid goes back into a URL path**, as the next page's cursor. It comes
out of a response body, which the vendor chooses freely, and `../../blocks/tip/height` is a
string too. `fullmatch` rather than `match` with `$`, which would accept a trailing newline."""

CAPABILITIES: Final = ChainCapabilities(
    chain_key=ChainKey.BITCOIN,
    decimals=BITCOIN_DECIMALS,
    max_addresses_per_call=MAX_ADDRESSES_PER_CALL,
)

BRANCH_CAP_MESSAGE: Final = (
    f"A branch of the extended key would need more than {MAX_ADDRESSES_PER_BRANCH} addresses "
    "to complete its gap limit, so the scan stopped rather than read without end."
)
"""Why a scan refused to go on (spec 031, R5). A fixed sentence: no key, address or index.

The only plausible cause in a single-owner tracker is an instance that reports history for
every address it is asked about, which is an answer this cannot use. Raised as
`ProviderResponseError`, the category for an answer that cannot be trusted.
"""

_BRANCHES: Final = (RECEIVE_BRANCH, CHANGE_BRANCH)
"""BIP44's two branches below an account key, scanned in this order."""


@dataclass(frozen=True, slots=True)
class AddressStats:
    """One address's two numbers, as the parser read them out of a response.

    A named pair rather than a `tuple[int, int | None]`, because `stats.pending` at a call
    site says what `parsed[1]` does not -- and because the second element is the one whose
    `None` carries meaning, which is exactly the element a positional tuple hides.

    `confirmed` is `chain_stats.funded_txo_sum - spent_txo_sum` and cannot be negative.
    `pending` is the same difference over `mempool_stats`, is **signed**, and is `None`
    when the response carried no mempool figures at all.

    `used` is whether the address has any transaction, confirmed or in the mempool: a
    `tx_count` above zero in either stats object (spec 031, R5). It is what an extended
    key's gap scan counts, and a balance read ignores it. An address emptied by a spend is
    used with a zero balance, which is exactly the case the balance alone cannot tell from
    an address nobody ever paid.
    """

    confirmed: int
    pending: int | None
    used: bool


@dataclass(frozen=True, slots=True)
class ChainStats:
    """An address's confirmed figures: how many transactions, and the two satoshi sums.

    What `address_history` checks a history against (R1), and compares before and after the
    paging: two reads that differ in any of the three mean a transaction confirmed while the
    history was being read. Equality is the dataclass's, over all three fields.
    """

    tx_count: int
    funded: int
    spent: int

    @property
    def balance(self) -> int:
        """`funded - spent`, the confirmed balance. Never negative: the parser refuses it."""
        return self.funded - self.spent


def parse_chain_stats(body: str | bytes, expected_address: str) -> ChainStats:
    """An address response's `chain_stats`, for the history's completeness check.

    The same parser `parse_address_response` uses for its confirmed half -- one reading of
    one object, so the two cannot drift -- with every refusal it makes for that half: the
    body, the echoed address, the stats object, either sum, a negative balance and the
    count. `mempool_stats` is not read: a pending transaction is not part of a history.

    Raises:
        ProviderResponseError: the rows of `parse_address_response`'s table about the body,
            the address and `chain_stats`.
    """
    return _chain_stats(require_json_object(body), expected_address)


def parse_address_response(body: str | bytes, expected_address: str) -> AddressStats:
    """Read the two balances out of an Esplora address response, or refuse it.

    Hand-written rather than a pydantic model, for the reason the module docstring gives
    at length: a `ValidationError` renders the input, and the input contains the owner's
    address. **No message raised from here contains the address or any part of the body**;
    each names a field and a type, which is the part anyone can act on.

    The refusals, and why each one is a refusal rather than a zero:

    | Condition | Why it is not survivable |
    |---|---|
    | the body is not JSON | an HTML holding page is not a balance |
    | the body is not an object | neither is a list or a number |
    | `address` is absent, mistyped, or a different address | see below |
    | `chain_stats` absent or not an object | the confirmed balance has nowhere to come from |
    | a sum absent, not an integer, or a `bool` | `1.0e8` out of `json.loads` is a float |
    | `spent_txo_sum` over `funded_txo_sum` | an address cannot spend what it never received |
    | a `tx_count` absent, not an integer, a `bool`, or negative | a gap scan reads "used" |
    | `mempool_stats` present but not an object | mistyped is an error; absent is not |

    **`tx_count` is required in each stats object that is read** (spec 031), on every read
    and not only an extended key's: one parser, one contract. Both vendors document it in
    both objects, so no conforming instance is refused for it. It is checked after the two
    sums of the same object, so a body with a bad sum is refused for the sum, as before.

    **The echoed address is checked against the one we asked about**, and the three ways
    it can be wrong are one refusal because they have one remedy. It catches a cache or a
    proxy answering about somebody else -- the correlation failure `align_balances` already
    refuses for a batch, which a single-address API can produce just as easily and which
    would otherwise be reported as somebody else's balance under this address's name.

    **An absent `mempool_stats` is not an error.** It yields `pending=None`, because an
    instance that does not report a mempool is one that cannot answer rather than one
    answering zero. That distinction is the entire reason `AddressBalance.pending` is
    `int | None`.

    Args:
        body: the response body, as text or as bytes.
        expected_address: the canonical address this response is supposed to be about.

    Returns:
        The confirmed balance in satoshis, the signed mempool delta or `None`, and whether
        the address has ever had a transaction.

    Raises:
        ProviderResponseError: any row of the table above.
    """
    document = require_json_object(body)
    chain = _chain_stats(document, expected_address)
    used = chain.tx_count > 0

    # `in` rather than `.get(...) is None`, so that an explicit null is a mistyped field
    # and reaches the refusal below rather than being read as "no mempool figures".
    pending: int | None = None
    if MEMPOOL_STATS in document:
        mempool_stats = _require_stats(document, MEMPOOL_STATS)
        pending = _require_delta(mempool_stats, MEMPOOL_STATS)
        # Read before the `or`, never inside it: a short-circuit would skip the check on
        # every address that already has a confirmed transaction.
        mempool_used = _require_count(mempool_stats, MEMPOOL_STATS, TX_COUNT) > 0
        used = used or mempool_used
    return AddressStats(confirmed=chain.balance, pending=pending, used=used)


def _chain_stats(document: Mapping[str, object], expected_address: str) -> ChainStats:
    """The echoed address and the confirmed half of an address response, or a refusal.

    The refusals, in the order a body meets them: an echo that is not the address asked
    about, a `chain_stats` that is not an object, either sum, a spent sum over the funded
    one, and the count. Both public parsers go through here, so they refuse identically.
    """
    if document.get(ADDRESS_FIELD) != expected_address:
        message = (
            "The response does not carry the 'address' it was asked about, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    chain_stats = _require_stats(document, CHAIN_STATS)
    funded = _require_sum(chain_stats, CHAIN_STATS, FUNDED_SUM)
    spent = _require_sum(chain_stats, CHAIN_STATS, SPENT_SUM)
    if spent > funded:
        message = (
            f"The response reports a larger {CHAIN_STATS}.{SPENT_SUM} than "
            f"{CHAIN_STATS}.{FUNDED_SUM}, which would make the confirmed balance negative."
        )
        raise ProviderResponseError(message)
    tx_count = _require_count(chain_stats, CHAIN_STATS, TX_COUNT)
    return ChainStats(tx_count=tx_count, funded=funded, spent=spent)


@dataclass(frozen=True, slots=True)
class ChainTransaction:
    """One confirmed transaction out of a history page: its id, its effect, and whether it
    could be read whole.

    `txid` is kept for two jobs and never leaves the provider: it de-duplicates across pages,
    and the last one on a page is the next page's cursor. `resolved` is false when a
    non-coinbase input carried no `prevout`, so its source -- and whether it spent from this
    address -- is unknown (R2); the effect then leaves that input out, and the history is
    reported `unresolved_input` rather than trusted.
    """

    txid: str
    effect: TxEffect
    resolved: bool


def parse_history_page(body: str | bytes, address: str) -> tuple[ChainTransaction, ...]:
    """Every transaction on one `/txs/chain` page, in the vendor's order, or a refusal.

    The net effect on `address` is R2: the outputs paying `address` minus the `prevout` of
    every input spending from it. A coinbase input has no `prevout` and is skipped -- it
    spends nothing. A non-coinbase input with no `prevout` is not a refusal but an unknown,
    carried as `resolved=False`.

    | Refused | Why |
    |---|---|
    | the body is not a JSON array, or an entry is not an object | the documented shape |
    | `txid` not 64 lower-case hex characters | it goes back into a URL as the cursor |
    | `status` not an object, or `status.confirmed` not `true` | a chain page is confirmed |
    | `status.block_time` not an `int` in `[0, LATEST_BLOCK_TIME]` | no time, no day (R3) |
    | `vin`/`vout` not arrays, or an entry of either not an object | the documented shape |
    | a `value` that is not an `int`, is a `bool`, or is negative | `1.0e8` is not satoshis |
    | a `prevout` present but not an object | mistyped is not "absent" |

    **Every amount is checked, not only those that touch `address`**: a vendor rendering one
    output as a float renders them all that way, and refusing only when it happened to be
    ours would make the refusal depend on the owner's holdings.

    No message names the address, a txid or an amount; each names a field and a type.

    Raises:
        ProviderResponseError: any row of the table above.
    """
    entries = decode_json(body)
    if not isinstance(entries, list):
        message = (
            f"The history page is a {type(entries).__name__} rather than the JSON array "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    return tuple(_chain_transaction(entry, address) for entry in entries)


def _chain_transaction(entry: object, address: str) -> ChainTransaction:
    """One history entry, read and checked. See `parse_history_page` for the refusals."""
    if not isinstance(entry, dict):
        message = (
            f"The history page carried an entry that is a {type(entry).__name__} rather "
            "than a transaction object."
        )
        raise ProviderResponseError(message)
    txid = entry.get(TXID_FIELD)
    if not isinstance(txid, str) or _TXID.fullmatch(txid) is None:
        message = (
            f"A transaction's {TXID_FIELD!r} is not 64 lower-case hexadecimal characters, so "
            "it cannot be followed as the next page's cursor."
        )
        raise ProviderResponseError(message)

    status = entry.get(STATUS_FIELD)
    if not isinstance(status, dict):
        message = (
            f"A transaction's {STATUS_FIELD!r} is a {type(status).__name__} rather than the "
            "object this endpoint documents."
        )
        raise ProviderResponseError(message)
    if status.get(CONFIRMED_FIELD) is not True:
        message = (
            "The confirmed-history page carried a transaction that is not confirmed, which "
            "this endpoint does not serve."
        )
        raise ProviderResponseError(message)
    occurred_at = _block_time(status.get(BLOCK_TIME_FIELD))

    received = 0
    for output in _require_entries(entry, VOUT_FIELD):
        value = _require_amount(output.get(VALUE_FIELD), f"{VOUT_FIELD}[].{VALUE_FIELD}")
        if output.get(SCRIPTPUBKEY_ADDRESS_FIELD) == address:
            received += value

    spent = 0
    resolved = True
    for txin in _require_entries(entry, VIN_FIELD):
        if txin.get(IS_COINBASE_FIELD) is True:
            continue
        prevout = txin.get(PREVOUT_FIELD)
        if prevout is None:
            resolved = False
            continue
        if not isinstance(prevout, dict):
            message = (
                f"A transaction's {VIN_FIELD}[].{PREVOUT_FIELD} is a {type(prevout).__name__} "
                "rather than the object this endpoint documents."
            )
            raise ProviderResponseError(message)
        value = _require_amount(
            prevout.get(VALUE_FIELD), f"{VIN_FIELD}[].{PREVOUT_FIELD}.{VALUE_FIELD}"
        )
        if prevout.get(SCRIPTPUBKEY_ADDRESS_FIELD) == address:
            spent += value

    return ChainTransaction(
        txid=txid,
        effect=TxEffect(occurred_at=occurred_at, delta=received - spent),
        resolved=resolved,
    )


def _block_time(value: object) -> datetime:
    """A block's Unix-seconds time as an aware UTC datetime, or a refusal.

    `int` only, `bool` refused, and bounded to what a `datetime` can hold (see
    `LATEST_BLOCK_TIME`), so the conversion below cannot raise. `fromtimestamp` of an `int`
    is exact. Absent and mistyped are one refusal: neither has a day to put the effect on.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= LATEST_BLOCK_TIME:
        message = (
            f"A confirmed transaction's {STATUS_FIELD}.{BLOCK_TIME_FIELD} is a "
            f"{type(value).__name__} that is not a Unix time in seconds."
        )
        raise ProviderResponseError(message)
    return datetime.fromtimestamp(value, UTC)


def _require_entries(entry: Mapping[str, object], field: str) -> list[Mapping[str, object]]:
    """A transaction's `vin` or `vout`: an array of objects, or a refusal naming the type."""
    items = entry.get(field)
    if not isinstance(items, list):
        message = (
            f"A transaction's {field!r} is a {type(items).__name__} rather than the array "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    objects: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            message = (
                f"A transaction's {field}[] carried a {type(item).__name__} rather than an object."
            )
            raise ProviderResponseError(message)
        objects.append(item)
    return objects


def _require_amount(value: object, field: str) -> int:
    """One output's or prevout's `value`: a whole, non-negative number of satoshis.

    The same refusal `_require_sum` makes, for the same reasons -- `json.loads` hands back
    whatever the vendor sent, and `True` is an `int` -- plus a negative value, which no
    output can carry. The message names the field and the type, never the value.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = (
            f"A transaction's {field} is a {type(value).__name__} that is not a whole, "
            "non-negative number of satoshis."
        )
        raise ProviderResponseError(message)
    return value


def _history_verdict(
    *,
    moved: bool,
    resolved: bool,
    ended: bool,
    collected: int,
    counted: int,
    summed: int,
    balance: int,
) -> HistoryIncomplete | None:
    """R1: `None` when the history proves itself complete, else the first reason it does not.

    In this order, because each earlier reason explains the later ones: a transaction that
    confirmed mid-read moves the count and the balance both; an input of unknown source
    makes the sum unknowable; and a history that did not end, or ended short, cannot be
    expected to sum to the balance. A history that did not end within the page cap is a
    count mismatch: the vendor served more pages than its own count allows.
    """
    if moved:
        return HistoryIncomplete.MOVED_DURING_READ
    if not resolved:
        return HistoryIncomplete.UNRESOLVED_INPUT
    if not ended or collected != counted:
        return HistoryIncomplete.COUNT_MISMATCH
    if summed != balance:
        return HistoryIncomplete.BALANCE_MISMATCH
    return None


def parse_tip_height(body: str | bytes) -> int:
    """The chain tip height out of `GET /blocks/tip/height`, or a refusal.

    The documented body is a plain integer, which is also valid JSON, so this shares
    `require_json_object`'s decoder rather than parsing digits by hand -- `json.loads` already
    rejects the Unicode digits that `str.isdigit` accepts and `int` then reads as a number.

    **A non-negative `int` specifically**, so that an instance answering with an HTML
    holding page, a JSON error object or `-1` is reported as unhealthy rather than as
    healthy-and-wrong. A `bool` is refused with everything else for the reason
    `domain/money.py` gives: it is an `int` subclass, so `True` would pass an
    `isinstance(..., int)` check and be read as height 1.

    Raises:
        ProviderResponseError: the body is not JSON, or is not a non-negative whole number.
    """
    height = decode_json(body)
    if isinstance(height, bool) or not isinstance(height, int) or height < 0:
        message = (
            "The tip height is not a non-negative whole number; the body parsed as "
            f"{type(height).__name__}."
        )
        raise ProviderResponseError(message)
    return height


def _require_stats(document: Mapping[str, object], field: str) -> Mapping[str, object]:
    """One stats object out of the response, refusing anything that is not an object.

    Absent and mistyped are one refusal deliberately: to a caller they are the same event
    -- this response has no usable figures under that name -- and splitting them would
    produce two messages with one remedy. The type is named, so the message still says
    which of the two happened.

    Raises:
        ProviderResponseError: the field is missing, or is not an object.
    """
    stats = document.get(field)
    if not isinstance(stats, dict):
        message = (
            f"The response field {field!r} is a {type(stats).__name__} rather than the "
            "object this endpoint documents."
        )
        raise ProviderResponseError(message)
    return stats


def _require_delta(stats: Mapping[str, object], field: str) -> int:
    """`funded_txo_sum - spent_txo_sum` out of one stats object, refusing anything else.

    Raises:
        ProviderResponseError: either sum is missing or is not a whole number of satoshis.
    """
    return _require_sum(stats, field, FUNDED_SUM) - _require_sum(stats, field, SPENT_SUM)


def _require_count(stats: Mapping[str, object], field: str, name: str) -> int:
    """One transaction count, refusing anything that is not a non-negative whole number.

    The same three refusals as `_require_sum` -- absent, mistyped, a `bool` -- for the same
    reasons, plus a negative count, which no chain can have. A count is not money, but it
    decides whether a gap scan stops (R5), so a value the parser cannot vouch for is no more
    acceptable here than in a sum. The message names the field and the type, never the value.
    """
    value = stats.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        message = (
            f"The response field {field}.{name} is a {type(value).__name__} rather than "
            "a whole number of transactions."
        )
        raise ProviderResponseError(message)
    if value < 0:
        message = f"The response field {field}.{name} is negative, which no count can be."
        raise ProviderResponseError(message)
    return value


def _require_sum(stats: Mapping[str, object], field: str, name: str) -> int:
    """One satoshi sum, refusing anything that is not a whole number of them.

    **`json.loads` returns whatever the vendor sent**, and the annotation above is a claim
    about what should arrive rather than a check that it did. A vendor rendering a balance
    as `1.0e8` produces a `float`, and a float that reached `AddressBalance.confirmed`
    would be money in binary floating point inside `providers/` -- where the AST ban in
    `backend/tests/security/test_no_float.py` cannot see it, because it reads source and
    this float has no literal.

    `bool` is refused with it: `True` is an `int` and would be read as one satoshi.

    The message names the field and the type. It never names the value, because the value
    is a figure about the owner's address.
    """
    value = stats.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        message = (
            f"The response field {field}.{name} is a {type(value).__name__} rather than "
            "a whole number of satoshis."
        )
        raise ProviderResponseError(message)
    return value


def _branch_key(parsed: ExtendedPublicKey, branch: int) -> DerivedKey:
    """The key of one branch below the account key, derived once per scan.

    BIP32 would have the caller move on to the next index when this one has no key, but a
    branch's index is fixed by BIP44: there is no other receive branch to move on to. So a
    key whose branch has no key cannot be scanned at all, and says so with the sentence for
    a key that holds no usable public key. The probability is below 2^-127; only an injected
    HMAC reaches this line.

    Raises:
        AddressInvalidError: `invalid_public_key`.
    """
    derived = derive_child(parsed, branch)
    if derived is None:
        raise AddressInvalidError(AddressRejection.INVALID_PUBLIC_KEY)
    return derived


def _next_derivable_child(branch_key: DerivedKey, index: int) -> DerivedKey:
    """The first child at or above `index` that BIP32 gives a key, skipping any that it does not.

    A skipped index is simply never returned, so it is never read, persisted or counted
    toward the gap (R5).

    Raises:
        ProviderResponseError: the branch ran out of non-hardened indices, with
            `BRANCH_CAP_MESSAGE`. Unreachable from a scan that started at zero, since the
            cap stops it a thousand addresses in; reachable only from a persisted address
            placed near 2^31, and then it is the same refusal for the same reason -- this
            branch cannot be completed.
    """
    while index < HARDENED_INDEX:
        child = derive_child(branch_key, index)
        if child is not None:
            return child
        index += 1
    raise ProviderResponseError(BRANCH_CAP_MESSAGE)


def _configured_candidates(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The two configured URLs, in the order they should be tried, as `(position, url)`.

    All the blank-and-duplicate reasoning lives in `endpoints.configured_endpoints`, which
    is where the second provider needs it too: a blank fallback means "one instance only",
    both blank means no instance is configured at all, and two URLs that are the same after
    trimming are one instance rather than a fallback onto the host that just refused us.

    What stays here is the one thing that is genuinely Bitcoin's: *which* settings hold the
    URLs, and in which order.
    """
    return (
        (PRIMARY, settings.bitcoin_esplora_url),
        (FALLBACK, settings.bitcoin_esplora_fallback_url),
    )


@register_chain_provider(ChainKey.BITCOIN)
class EsploraProvider:
    """Reads Bitcoin balances from an Esplora instance, falling back to a second one.

    Satisfies `ChainProvider` structurally, checked by `mypy --strict` rather than by
    `isinstance`, and `ChainProviderFactory` by taking the shared client as its only
    positional argument -- which is what lets the registry build it with nothing but a
    client. Also satisfies `ExtendedKeyScanner`, the one chain provider that does (spec 031),
    and `TransactionHistoryReader` (spec 038).
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client and read the configuration once.

        `settings` is a keyword with a default rather than a required argument, because
        `ChainProviderFactory` is `Callable[[httpx.AsyncClient], ChainProvider]` and a
        second required parameter would take this class out of that type. A test passes a
        `Settings` built in the test rather than monkeypatching the environment, which is
        also how it gets a fallback URL it can assert against.

        The configuration is read **once, here**, rather than per request: a provider
        whose base URL could change between two addresses of one call would produce a
        result set read from two different chains with nothing saying so.
        """
        resolved = settings if settings is not None else get_settings()
        self._client = client
        self._network = BitcoinNetwork(resolved.bitcoin_network)
        self._instances = EndpointSet.configured(
            client, _configured_candidates(resolved), vendor=VENDOR
        )

    @property
    def capabilities(self) -> ChainCapabilities:
        """Eight decimals, one address per call. Constant for the life of the instance."""
        return CAPABILITIES

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Decide whether `raw` is an address this instance can be asked about, offline.

        Two questions, and the second is the one that is new here. Whether the string is a
        Bitcoin address at all is `domain.chains.validate_address`'s, and this delegates
        rather than reimplementing it. Which *network* it is on is `bitcoin_network_of`'s,
        and it matters because an Esplora instance serves exactly one network: an address
        from another one either gets an undocumented error -- neither vendor says which --
        or, far worse, a balance read from the wrong chain, which is a number rather than
        an error and which nothing downstream can tell from a right one.

        Synchronous, and it must stay synchronous: it opens no socket and reads no clock,
        which is what lets a caller tell a mistyped address from an unreachable API
        without a round trip.

        Raises:
            AddressInvalidError: not a Bitcoin address, or an address on a Bitcoin network
                this provider is not configured for. The rejection names a reason and
                never contains `raw`.
        """
        validated = validate_chain_address(ChainKey.BITCOIN.value, raw)
        if bitcoin_network_of(validated.canonical) is not self._network:
            raise AddressInvalidError(AddressRejection.WRONG_NETWORK)
        return validated

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Read every address, in order, one request each.

        **Every address is validated before any URL is built, and that ordering is a
        security property rather than tidiness.** The address arrives from a database
        column; interpolating a database value into a URL path is the shape of a
        path-traversal bug, and the only thing standing between it and
        `GET /address/../../blocks/tip/height` is that somebody validated it first. After
        `validate_address` the string is bech32 or base58check -- alphanumeric, with no
        slash, no dot and no percent-escape, by construction rather than by inspection --
        so `ADDRESS_PATH.format` cannot produce a path that leaves the endpoint.

        Validating the whole list up front rather than address by address is the other
        half of it: a bad address in position twelve costs no requests at all, rather than
        eleven reads at a vendor whose rate limit is unpublished and enforced by ban.

        Sequential, never `gather`: see the module docstring. Sticky failover, also there.

        **A duplicate is refused here, before any request, and not only by
        `align_balances`.** `align_balances` does refuse it, and remains the enforcement
        point for every other caller -- but it runs last, so leaving it as the only check
        meant twenty duplicated addresses cost twenty requests at a vendor whose limit is
        unpublished and enforced by a ban, and then raised. That also contradicted the
        paragraph above it, which promises that a bad address costs no requests at all. A
        promise that holds for one kind of bad address is not the promise it appears to be.

        Raises:
            AddressInvalidError: one of the addresses is not one this instance can read.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance refused the request, or answered with
                something that cannot be trusted.
            ValueError: the same address was requested twice.
        """
        canonical = [self.validate_address(raw).canonical for raw in addresses]
        distinct = set(canonical)
        if len(distinct) != len(canonical):
            # Deliberately the same shape of message `align_balances` raises, because it
            # is the same mistake; what differs is only that this one costs no requests.
            message = (
                f"fetch_balances was given {len(canonical)} addresses "
                f"of which only {len(distinct)} are distinct"
            )
            raise ValueError(message)

        confirmed: dict[str, int] = {}
        pending: dict[str, int] = {}
        # Sticky within this call, and only within it: the index the next address starts
        # from, which is the one that last answered.
        start = 0
        for address in canonical:
            stats, start = await self._read_address(address, start)
            confirmed[address] = stats.confirmed
            if stats.pending is not None:
                pending[address] = stats.pending

        # `pending` is handed over even when it is empty: `align_balances` reads a missing
        # address as "this chain did not say", which is exactly what an empty mapping
        # means here and is not the same statement as a zero.
        return align_balances(canonical, confirmed, decimals=BITCOIN_DECIMALS, pending=pending)

    async def scan_extended_key(
        self, key: str, known: Sequence[KnownDerivedAddress]
    ) -> ExtendedKeyScan:
        """Read every address an extended public key's gap-limit scan reaches (spec 031).

        Satisfies `ExtendedKeyScanner`. The order of work, and why each step is where it is:

        1. **Everything that can be refused is refused before the first request.** The key
           is parsed; its network family is checked against `PORTFOLIO_BITCOIN_NETWORK`
           (R3), so a `tpub` on a mainnet instance is `wrong_network` and costs nothing;
           every persisted address is validated as `fetch_balances` validates one; and both
           branch keys are derived. The persisted addresses come out of a database column
           and go into a URL path, which is the same reason `fetch_balances` gives for
           validating first.
        2. **Per branch, receive then change, every persisted address is read**, used or
           not, in index order (R5): funds can arrive at any of them.
        3. **Then the branch is extended**, deriving only above its highest persisted index
           (R6), by however many addresses `addresses_to_extend` asks for, until it asks for
           none. An index BIP32 gives no key is skipped: never read, never returned, never
           counted toward the gap. A branch that would pass `MAX_ADDRESSES_PER_BRANCH`
           raises with `BRANCH_CAP_MESSAGE` instead of reading on.

        **Every read goes through the path `fetch_balances` uses** -- `_read_address`: the
        same endpoint label, the same client, so every request, retries included, acquires
        the host limiter -- **sequentially**, for the reason the module docstring gives,
        and with failover sticky for the whole scan, both branches included. A first scan
        is at least forty requests, so at least forty seconds against one host.

        `used` on each result is the persisted flag or the vendor's answer, whichever says
        used: an address once used stays used (R5), even if an instance that has pruned its
        history now reports nothing for it.

        **Nothing here logs**, as everywhere in this module: the key, every address and
        every index are the owner's holdings. The caller logs counts.

        Raises:
            AddressInvalidError: the key does not parse (with the parser's reason), belongs
                to the other network family (`wrong_network`), or has a branch with no key
                (`invalid_public_key`); or a persisted address does not validate, is not in
                its canonical form (`malformed`), or is on another network
                (`wrong_network`). All of them before any request.
            ValueError: a persisted address sits outside the two branches or the
                non-hardened range, or two share a position. A caller's mistake, never data
                a vendor sent; the table's constraints make it unreachable from the sync.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance answered with something that cannot be
                trusted, or a branch would pass the cap.
        """
        parsed = parse_extended_public_key(key)
        if parsed.network_family is not NETWORK_FAMILY_BY_NETWORK[self._network]:
            raise AddressInvalidError(AddressRejection.WRONG_NETWORK)
        known_by_branch = self._known_by_branch(known, parsed.script_type)
        branch_keys = tuple(_branch_key(parsed, branch) for branch in _BRANCHES)

        scanned: list[ScannedAddress] = []
        # Sticky for the whole scan, as within one `fetch_balances` call.
        start = 0
        for branch, branch_key in zip(_BRANCHES, branch_keys, strict=True):
            persisted = known_by_branch[branch]
            on_branch: list[ScannedAddress] = []
            for entry in persisted:
                stats, start = await self._read_address(entry.address, start)
                on_branch.append(
                    ScannedAddress(
                        branch=branch,
                        index=entry.index,
                        address=entry.address,
                        used=entry.used or stats.used,
                        confirmed=stats.confirmed,
                        pending=stats.pending,
                    )
                )

            next_index = persisted[-1].index + 1 if persisted else 0
            while missing := addresses_to_extend([address.used for address in on_branch]):
                for _ in range(missing):
                    if len(on_branch) >= MAX_ADDRESSES_PER_BRANCH:
                        raise ProviderResponseError(BRANCH_CAP_MESSAGE)
                    child = _next_derivable_child(branch_key, next_index)
                    next_index = child.index + 1
                    address = address_of(child.public_key, parsed.script_type, self._network)
                    stats, start = await self._read_address(address, start)
                    on_branch.append(
                        ScannedAddress(
                            branch=branch,
                            index=child.index,
                            address=address,
                            used=stats.used,
                            confirmed=stats.confirmed,
                            pending=stats.pending,
                        )
                    )
            scanned.extend(on_branch)

        return ExtendedKeyScan(addresses=tuple(scanned), decimals=BITCOIN_DECIMALS)

    async def address_history(self, address: str) -> AddressHistory:
        """Every confirmed transaction's effect on `address`, oldest first, checked by R1.

        Satisfies `TransactionHistoryReader` (spec 038). The order of work:

        1. **The address is validated before any URL is built**, as `fetch_balances` does and
           for its reason: it goes into a path.
        2. **`GET /address/:a`**, for `chain_stats`: the count and the balance the history
           will be checked against.
        3. **`GET /address/:a/txs/chain`, then `/txs/chain/{last txid}`** until a page comes
           back empty, at most `tx_count // 25 + 2` pages. Each txid is checked before it is
           put back into a path. A txid seen twice is kept once.
        4. **`GET /address/:a` again.** Any difference from step 2 is a transaction that
           confirmed during the read.

        **An empty page is not proof of the end**: an unknown or reorged-out cursor also
        answers `200 []`. So the history is complete only when it proves it (R1) -- the
        distinct txids number `tx_count`, the effects sum to `funded - spent`, and the two
        stats reads agree. Otherwise `incomplete` says why (see `_history_verdict` for the
        order), `effects` is what was collected, and nothing may store it.

        Every read is sequential and goes through the host limiter, with failover sticky for
        the whole history, stats reads included: a host that refused page three is not asked
        for page four. A history of N transactions costs about N/25 + 3 requests.

        **Nothing here logs**, as everywhere in this module: the address, the txids and the
        amounts are the owner's holdings. The caller logs the reason and a count.

        Raises:
            AddressInvalidError: not a Bitcoin address, or one on another network. Before
                any request.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance refused the request, or answered with
                something that cannot be trusted (see `parse_history_page`).
        """
        canonical = self.validate_address(address).canonical
        # Sticky for the whole history, as within one `fetch_balances` call.
        start = 0
        before, start = await self._read_chain_stats(canonical, start)

        # Insertion-ordered, so newest first as the pages are; a repeat keeps the first.
        collected: dict[str, ChainTransaction] = {}
        path = HISTORY_PATH.format(address=canonical)
        ended = False
        for _ in range(before.tx_count // HISTORY_PAGE_SIZE + 2):
            body, start = await self._instances.read(path, ADDRESS_HISTORY, start)
            page = parse_history_page(body, canonical)
            if not page:
                ended = True
                break
            for transaction in page:
                collected.setdefault(transaction.txid, transaction)
            path = HISTORY_PAGE_PATH.format(address=canonical, txid=page[-1].txid)

        after, start = await self._read_chain_stats(canonical, start)

        oldest_first = tuple(reversed(collected.values()))
        effects = tuple(transaction.effect for transaction in oldest_first)
        incomplete = _history_verdict(
            moved=before != after,
            resolved=all(transaction.resolved for transaction in oldest_first),
            ended=ended,
            collected=len(collected),
            counted=before.tx_count,
            summed=sum(effect.delta for effect in effects),
            balance=before.balance,
        )
        return AddressHistory(
            address=canonical,
            balance=before.balance,
            decimals=BITCOIN_DECIMALS,
            effects=effects,
            incomplete=incomplete,
        )

    async def _read_chain_stats(self, address: str, start: int) -> tuple[ChainStats, int]:
        """`GET /address/:address` for its `chain_stats`, from the first instance that answers.

        The same request `_read_address` makes, under the same label -- it is the same read
        of the same endpoint -- parsed for the three figures a history is checked against.
        """
        body, start = await self._instances.read(
            ADDRESS_PATH.format(address=address), ADDRESS_BALANCE, start
        )
        return parse_chain_stats(body, address), start

    async def _read_address(self, address: str, start: int) -> tuple[AddressStats, int]:
        """One address read: `GET /address/:address` from the first instance that answers.

        The one per-address path both `fetch_balances` and `scan_extended_key` take, so the
        two cannot drift apart in their endpoint label, their limiter or their parser.
        `address` must already be validated or derived: it goes into the path verbatim.
        Returns the parsed figures and the index of the instance that answered, which the
        caller carries into its next read.
        """
        body, start = await self._instances.read(
            ADDRESS_PATH.format(address=address), ADDRESS_BALANCE, start
        )
        return parse_address_response(body, address), start

    def _known_by_branch(
        self, known: Sequence[KnownDerivedAddress], script_type: ScriptType
    ) -> dict[int, list[KnownDerivedAddress]]:
        """The persisted addresses, validated, grouped by branch, each group by index.

        Validated as `validate_address` validates a registered address, with one allowance
        that is not a loosening. A P2PKH or P2SH-P2WPKH address derived for regtest is byte
        for byte the testnet one, so `bitcoin_network_of` answers `TESTNET` for it
        (`domain/addresses.py`, `P2SH_VERSION_BYTE_BY_NETWORK`), and that is the answer
        expected here under regtest. A bech32 address carries `bcrt` and is held to it.

        A persisted address on another network than the configured one means the setting
        changed under a wallet whose addresses were derived for the old value. That is
        `wrong_network`, the same refusal a registered address gets, rather than a read of
        one chain's addresses against another chain's instance.

        Raises:
            AddressInvalidError, ValueError: see `scan_extended_key`.
        """
        expected_network = (
            BitcoinNetwork.TESTNET
            if self._network is BitcoinNetwork.REGTEST and script_type is not ScriptType.P2WPKH
            else self._network
        )
        by_branch: dict[int, list[KnownDerivedAddress]] = {branch: [] for branch in _BRANCHES}
        positions: set[tuple[int, int]] = set()
        for entry in known:
            if entry.branch not in by_branch or not 0 <= entry.index < HARDENED_INDEX:
                message = (
                    "A persisted derived address sits outside the receive and change "
                    "branches or the non-hardened index range."
                )
                raise ValueError(message)
            if (entry.branch, entry.index) in positions:
                message = "Two persisted derived addresses share one branch and index."
                raise ValueError(message)
            positions.add((entry.branch, entry.index))

            validated = validate_chain_address(ChainKey.BITCOIN.value, entry.address)
            if validated.canonical != entry.address:
                raise AddressInvalidError(AddressRejection.MALFORMED)
            if bitcoin_network_of(validated.canonical) is not expected_network:
                raise AddressInvalidError(AddressRejection.WRONG_NETWORK)
            by_branch[entry.branch].append(entry)

        for entries in by_branch.values():
            entries.sort(key=lambda entry: entry.index)
        return by_branch

    async def health(self) -> ProviderHealth:
        """Whether either instance is answering, without reading any address.

        `GET /blocks/tip/height` is documented, cheap, and names nothing. The height is
        parsed rather than merely received, so an instance serving an HTML holding page
        with a 200 is unhealthy rather than healthy-and-wrong.

        **This does not raise**, so that an operations view can report a broken vendor
        without having to catch anything, and `detail` carries a reason and at most which
        position answered -- never a URL, never a body, never an address.

        That statement used to carry a residual, and the residual turned out to be a
        defect rather than a footnote. A base URL with no scheme or no host reaches
        `client.get` as a bare `ValueError` out of `urllib` -- not an `httpx` exception at
        all, so nothing here could have caught it by type, and "never raises" was simply
        untrue for three plausible typos. It is closed upstream instead:
        `config.provider_url_violation` refuses such a URL at startup, so no running
        application holds one. Nothing is caught here, because catching an exception that
        cannot arrive is a branch no test can reach and a claim no reader can check.
        """
        reason = "no endpoint configured"
        for instance in self._instances.endpoints:
            failure = await self._probe(instance)
            if failure is None:
                return ProviderHealth(
                    chain_key=ChainKey.BITCOIN, healthy=True, detail=instance.position
                )
            reason = failure
        return ProviderHealth(chain_key=ChainKey.BITCOIN, healthy=False, detail=reason)

    async def _probe(self, instance: Endpoint) -> str | None:
        """Ask one instance for the tip height. `None` if it answered, else why not.

        A string rather than an exception, because the caller's job is to try the next one
        and then report -- and because every one of these strings is rendered to an
        operator, so they are built here where the no-URL, no-body, no-address rule is
        visible rather than assembled from an exception somewhere else.
        """
        try:
            response = await self._client.get(
                instance.url(TIP_HEIGHT_PATH),
                extensions={ENDPOINT_EXTENSION: BLOCK_TIP_HEIGHT},
            )
        except httpx.TransportError as error:
            # The class name, not `str(error)`: `httpx` puts the request's URL into some
            # of its messages, and the URL carries the deployment.
            return f"{instance.position}: {type(error).__name__}"
        if response.status_code != HTTPStatus.OK:
            return f"{instance.position}: HTTP {response.status_code}"
        try:
            parse_tip_height(response.text)
        except ProviderResponseError:
            return f"{instance.position}: unreadable tip height"
        return None
