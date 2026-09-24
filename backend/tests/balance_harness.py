"""The seam every balance-sync test drives the providers through, and the SQL it reads back.

Three things live here so that the four suites #10 adds do not each invent their own.

## The seam is `ChainProviderRegistry.create`, and that choice is deliberate

`providers/registry.py` documents `create(chain_key, client)` as *the* construction point:
the registry holds factories rather than instances precisely because a provider is bound to
the shared `httpx.AsyncClient`, and nothing above `providers/` may build one any other way.
Patching that one bound method therefore intercepts every path a caller can take to a
provider -- `get_chain_provider`, the registry directly, or a module-level import of either
-- without this file having to know which spelling the service chose.

The alternative, an `httpx.MockTransport` under the real providers, was rejected for these
suites: it would make a test of the sync service also a test of two vendors' document
shapes, which `tests/providers/chains/` already owns, and a change to either vendor's JSON
would then break tests about failure isolation. `tests/api/test_balances.py` still drives
the whole stack; what it stubs is the vendor, not the service.

**Every stub counts its calls**, and every test that relies on a provider having been
reached asserts that count. A harness that silently stopped intercepting would otherwise
turn "the other chain still succeeded" into "no chain was ever asked", which passes the
same assertion for the opposite reason.

## Nothing here writes a mainnet address

The addresses come from `tests/address_vectors.py`, which is testnet, signet and regtest
only and is scanned mechanically by `tests/security/test_address_logging.py`. Rule 3
applies to a fixture exactly as it applies to a source file.

## The raw SQL is not laziness

The three tables #10 adds are read back through `text()` rather than through the mapped
classes on purpose, for the reason `tests/db/test_wallets_repository.py` gives about
`wallets`: a read through the same ORM identity map that did the write can be answered out
of the session rather than off the disk, and the question these suites ask is what is *in
the file*. It also keeps the fixtures independent of the repository method names, so a
renamed method breaks the tests that are about that method and not the ones that are about
the schema.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from portfolio.domain.chains import ChainKey, validate_address
from portfolio.providers.base import (
    AddressBalance,
    ChainCapabilities,
    ChainProvider,
    ProviderHealth,
    align_balances,
)
from portfolio.providers.errors import UnknownChainError
from portfolio.providers.registry import CHAIN_PROVIDERS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    import httpx
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.domain.chains import ValidatedAddress

#: Both chains count in 1e-8. Read off the seeded `assets` rows rather than invented: the
#: snapshot stores its own `decimals`, and a test that used a different number from the one
#: the provider declares would be asserting about a conversion nothing performs.
CHAIN_DECIMALS: Final[Mapping[ChainKey, int]] = {ChainKey.BITCOIN: 8, ChainKey.KASPA: 8}

#: `Number.MAX_SAFE_INTEGER`, written out. The spec's arithmetic turns on this number, so
#: it is a constant here rather than a literal buried in one assertion.
MAX_SAFE_INTEGER: Final = 9007199254740991

#: 2.87e18 sompi -- the whole KAS supply in base units, from the spec's own calculation.
#: Three hundred times `MAX_SAFE_INTEGER`, so a value that went through a double comes back
#: with different digits rather than merely a different type.
KASPA_SUPPLY_SOMPI: Final = 2_870_000_000_000_000_000


class StubChainProvider:
    """A chain provider that answers from a dictionary, fails on command, and counts.

    Structural conformance only -- it does not inherit from `ChainProvider` -- which is
    what `_CONFORMS` at the bottom of this module makes `mypy --strict` check, the same
    arrangement `tests/providers/fakes.py` uses and for the same reason.

    `raises` is a `BaseException` rather than an exception *class* so that a test can hand
    over an instance carrying the message it wants to find in `sync_run_chains.detail`.
    It is raised on every call, because a provider that failed once and then succeeded
    would make "the run recorded a failure" ambiguous about which call it recorded.

    `on_fetch` is awaited **before** the balances are produced and before `raises` is
    honoured. That ordering is what lets a test assert on the database from inside the
    provider call -- which is the only way to prove a row existed *before* the provider
    was reached, rather than proving it exists afterwards.
    """

    def __init__(
        self,
        chain_key: ChainKey = ChainKey.BITCOIN,
        balances: Mapping[str, int] | None = None,
        *,
        pending: Mapping[str, int] | None = None,
        raises: BaseException | None = None,
        max_addresses_per_call: int = 10,
        on_fetch: Callable[[Sequence[str]], Awaitable[None]] | None = None,
    ) -> None:
        self._balances = dict(balances or {})
        self._pending = dict(pending) if pending is not None else None
        self.raises = raises
        self.on_fetch = on_fetch
        #: Every batch this provider was asked for, in order. Asserted rather than
        #: inspected: "the other chain still ran" is a claim about a call being made.
        self.calls: list[tuple[str, ...]] = []
        self._capabilities = ChainCapabilities(
            chain_key=chain_key,
            decimals=CHAIN_DECIMALS[chain_key],
            max_addresses_per_call=max_addresses_per_call,
        )

    @property
    def capabilities(self) -> ChainCapabilities:
        return self._capabilities

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Delegate, so the stub cannot disagree with the domain about what is valid."""
        return validate_address(self._capabilities.chain_key, raw)

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Answer about exactly what was asked, through the shared alignment rule."""
        self.calls.append(tuple(addresses))
        if self.on_fetch is not None:
            await self.on_fetch(addresses)
        if self.raises is not None:
            raise self.raises
        found = {
            address: self._balances[address] for address in addresses if address in self._balances
        }
        pending = (
            {address: self._pending[address] for address in addresses if address in self._pending}
            if self._pending is not None
            else None
        )
        return align_balances(
            addresses,
            found,
            decimals=self._capabilities.decimals,
            pending=pending,
        )

    async def health(self) -> ProviderHealth:
        healthy = self.raises is None
        return ProviderHealth(
            chain_key=self._capabilities.chain_key,
            healthy=healthy,
            detail=None if healthy else "stubbed failure",
        )


_CONFORMS: ChainProvider = StubChainProvider()
"""`mypy --strict` is the assertion. Do not replace with `isinstance`; see `fakes.py`."""


class RegistryStub:
    """The record of what `ChainProviderRegistry.create` was asked for and what it gave back.

    Kept separate from the providers so that a test can ask two different questions: which
    chains were *constructed* (this object) and which addresses were *read* (each stub's
    `calls`). A chain whose provider was built and never asked is a real bug shape -- an
    empty address group reaching `fetch_balances` with nothing in it -- and collapsing the
    two records would hide it.
    """

    def __init__(self, providers: Mapping[ChainKey, StubChainProvider]) -> None:
        self.providers = dict(providers)
        self.created: list[str] = []

    def create(self, chain_key: str, client: httpx.AsyncClient) -> ChainProvider:
        """Stand in for the registry: build nothing, hand back the stub for this key."""
        del client  # The stub opens no socket, which is the whole point of it.
        self.created.append(chain_key)
        try:
            key = ChainKey(chain_key)
        except ValueError:
            raise UnknownChainError(chain_key, self.registered_keys()) from None
        provider = self.providers.get(key)
        if provider is None:
            # The same refusal the real registry gives for a chain nobody claimed, which
            # is what `sync_run_chains.error_kind = 'unknown_chain'` is produced from.
            raise UnknownChainError(chain_key, self.registered_keys())
        return provider

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(sorted(key.value for key in self.providers))


def stub_chain_providers(
    monkeypatch: pytest.MonkeyPatch,
    providers: Mapping[ChainKey, StubChainProvider],
) -> RegistryStub:
    """Replace the process-wide registry's `create` for the duration of one test.

    Patched on the shared instance rather than on the class, so that nothing outside this
    test can see the substitution and `monkeypatch` puts the bound method back afterwards.

    Returns the record, so that a test can assert the seam was actually used. **Every test
    that depends on a provider having been reached must make that assertion**: a service
    that stopped going through the registry would otherwise make "the failing chain wrote
    no snapshot" true for the wrong reason.
    """
    stub = RegistryStub(providers)
    monkeypatch.setattr(CHAIN_PROVIDERS, "create", stub.create)
    return stub


# --------------------------------------------------------------------------------------
# Reading the three new tables back off the disk
# --------------------------------------------------------------------------------------

SYNC_RUNS_SQL: Final = (
    "SELECT id, trigger, status, started_at, finished_at, duration_ms, "
    "wallets_total, wallets_succeeded, wallets_failed FROM sync_runs ORDER BY id"
)

SYNC_RUN_CHAINS_SQL: Final = (
    "SELECT sync_run_id, chain_key, status, wallets_read, error_kind, detail "
    "FROM sync_run_chains ORDER BY sync_run_id, chain_key"
)

SNAPSHOTS_SQL: Final = (
    "SELECT id, wallet_id, sync_run_id, confirmed, pending, decimals, observed_at "
    "FROM balance_snapshots ORDER BY id"
)


async def rows_of(session: AsyncSession, sql: str) -> list[dict[str, Any]]:
    """One of the statements above, as a list of plain dictionaries."""
    result = await session.execute(text(sql))
    return [dict(row) for row in result.mappings().all()]


async def sync_runs(sessionmaker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Every `sync_runs` row, oldest first, as SQLite has it."""
    async with sessionmaker() as session:
        return await rows_of(session, SYNC_RUNS_SQL)


async def sync_run_chains(sessionmaker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Every `sync_run_chains` row, ordered by run and then by chain key."""
    async with sessionmaker() as session:
        return await rows_of(session, SYNC_RUN_CHAINS_SQL)


async def snapshots(sessionmaker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Every `balance_snapshots` row, in insertion order."""
    async with sessionmaker() as session:
        return await rows_of(session, SNAPSHOTS_SQL)


def sqlite_timestamp(moment: datetime) -> str:
    """A datetime as SQLAlchemy's SQLite `DATETIME` writes it: fixed width, six fractional digits.

    Hand-written rather than reached for through `str()`, and the width is the whole point.
    The spec's argument that `observed_at` may be compared in SQL rests on every value in
    the column being the same number of characters -- `"2026-09-24 00:00:00.000000"` --
    because that is what makes lexicographic order chronological order. A fixture that
    inserted `"2026-09-24 00:00:00"` would be six characters short, would sort *before* an
    equal instant written properly, and would make a passing ordering test a lie about the
    schema.

    `tests/db/test_balances_repository.py::test_the_fixture_timestamp_format_is_the_one_the_writer_uses`
    checks this function against a row the application itself wrote, so the claim above is
    verified rather than asserted.

    The value is converted to UTC first, for the reason `UtcDateTime` converts on bind: a
    fixture written in another offset would be stored at the wrong instant and every
    ordering assertion built on it would be about a timeline nothing else shares.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
