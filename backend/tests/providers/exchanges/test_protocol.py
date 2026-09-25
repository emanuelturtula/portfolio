"""Criterion 2's protocol: what `ExchangeProvider` requires, and that the check on it can fail.

The same two halves as `tests/providers/test_protocol.py`. `fakes.py` carries
`_CONFORMS: ExchangeProvider = FakeExchangeProvider()` and the gate's `mypy --strict` decides
whether it type checks; `test_mypy_rejects_an_exchange_provider_with_the_wrong_signature`
plants a broken provider beside a conforming one and shows the same check rejecting it, in
one run, so a failure for an unrelated reason shows up as a failure of the control.

The second half of this module drives the fake through several pages, which is where the
fetch contract meets pagination: an empty venue, a single page, several pages, and a venue
whose cursor stops advancing -- the case that would otherwise loop a Raspberry Pi forever.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.exchanges import FillSide
from portfolio.providers.exchanges.base import (
    ExchangeProvider,
    FillWindow,
    NormalizedFill,
)
from portfolio.providers.exchanges.errors import ExchangeSchemaError
from tests.providers.exchanges.fakes import FAKE_PAGE_SIZE, FakeExchangeProvider
from tests.providers.test_protocol import BLOCKING_IMPORTS, SOURCE_ROOT, imported_roots, run_mypy

if TYPE_CHECKING:
    from portfolio.providers.exchanges.base import FillPage

EXCHANGES_DIR: Final = SOURCE_ROOT / "portfolio" / "providers" / "exchanges"

#: Pinned as a literal, not derived from the protocol, for the reason the chain test gives:
#: a set derived from the thing it describes shrinks along with it.
EXPECTED_PROTOCOL_MEMBERS: Final = frozenset(
    {"capabilities", "fetch_fill_page", "candidate_symbols"}
)

#: The modules of the seam that must stay pure: no socket, no event loop, no HTTP client.
#: A venue module (#13, #14) is where I/O belongs, and none exists yet.
PURE_SEAM_MODULES: Final = ("base.py", "errors.py", "signing.py", "credentials.py")

WINDOW: Final = FillWindow(
    since=datetime(2026, 9, 1, tzinfo=UTC), until=datetime(2026, 9, 8, tzinfo=UTC)
)


def fill(trade_id: str) -> NormalizedFill:
    return NormalizedFill(
        external_trade_id=trade_id,
        external_order_id=None,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side=FillSide.SELL,
        quantity=Decimal("0.5"),
        price=Decimal("86000.10"),
        quote_quantity=Decimal("43000.05"),
        quote_quantity_derived=False,
        fee_amount=Decimal("0"),
        fee_asset=None,
        executed_at=datetime(2026, 9, 2, tzinfo=UTC),
        raw_payload="{}",
    )


async def drain(provider: FakeExchangeProvider, *, max_pages: int = 10) -> list[FillPage]:
    """Follow the cursor to the end, the way #15's loop will -- with a page budget.

    The budget is the test's own safety net and not the thing under test: the guard in
    `assemble_fill_page` is supposed to fire long before it. A test that relied on the
    budget would report a hang as an assertion failure and hide which guard was missing.
    """
    pages: list[FillPage] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = await provider.fetch_fill_page(WINDOW, cursor=cursor, symbol=None)
        pages.append(page)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor
    message = f"pagination did not end within {max_pages} pages"
    raise AssertionError(message)


# --------------------------------------------------------------------------------------
# The members, and the shape of each one
# --------------------------------------------------------------------------------------


def test_the_protocol_members_are_the_pinned_set() -> None:
    attrs: object = getattr(ExchangeProvider, "__protocol_attrs__", None)
    assert isinstance(attrs, set), "ExchangeProvider is not a Protocol, or typing changed"

    assert frozenset(str(name) for name in attrs) == EXPECTED_PROTOCOL_MEMBERS
    assert getattr(ExchangeProvider, "_is_protocol", False) is True


def test_the_two_members_that_talk_to_a_venue_are_coroutines() -> None:
    """`fetch_fill_page` returns one page, not an async generator: the sync commits between."""
    assert inspect.iscoroutinefunction(ExchangeProvider.fetch_fill_page)
    assert inspect.iscoroutinefunction(ExchangeProvider.candidate_symbols)
    assert not inspect.isasyncgenfunction(ExchangeProvider.fetch_fill_page)
    assert isinstance(ExchangeProvider.capabilities, property)


def test_cursor_and_symbol_are_keyword_only() -> None:
    """Two optional strings side by side are exactly the arguments a caller swaps by accident."""
    parameters = inspect.signature(ExchangeProvider.fetch_fill_page).parameters

    assert list(parameters) == ["self", "window", "cursor", "symbol"]
    assert parameters["cursor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["symbol"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_protocol_is_not_runtime_checkable() -> None:
    assert getattr(ExchangeProvider, "_is_runtime_protocol", False) is False

    with pytest.raises(TypeError, match=r"(?i)runtime"):
        isinstance(FakeExchangeProvider(), ExchangeProvider)  # type: ignore[misc]


@pytest.mark.parametrize("module", PURE_SEAM_MODULES)
def test_the_exchange_seam_imports_nothing_that_does_io(module: str) -> None:
    path = EXCHANGES_DIR / module
    assert path.is_file(), f"{path} does not exist; the module was renamed or never landed"

    roots = imported_roots(path)

    assert roots, f"{module} imports nothing at all, so the scan saw nothing"
    assert roots & BLOCKING_IMPORTS == set(), f"{module} imports {sorted(roots & BLOCKING_IMPORTS)}"


# --------------------------------------------------------------------------------------
# The static check, proven able to fail
# --------------------------------------------------------------------------------------

CONFORMING_PROVIDER: Final = '''\
"""An exchange provider that satisfies the protocol. The control for the broken one."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.base import (
    CursorKind,
    ExchangeCapabilities,
    ExchangeProvider,
    FillPage,
    FillWindow,
    RateLimit,
    assemble_fill_page,
)


class ConformingProvider:
    @property
    def capabilities(self) -> ExchangeCapabilities:
        return ExchangeCapabilities(
            exchange_key=ExchangeKey.BINGX,
            retention=None,
            max_query_window=timedelta(days=7),
            page_size=100,
            cursor_kind=CursorKind.NONE,
            rate_limit=RateLimit(max_requests=10, per_ms=1000),
            requires_symbol=False,
        )

    async def fetch_fill_page(
        self, window: FillWindow, *, cursor: str | None, symbol: str | None
    ) -> FillPage:
        return assemble_fill_page(
            window,
            (),
            capabilities=self.capabilities,
            cursor=cursor,
            next_cursor=None,
            symbol=symbol,
        )

    async def candidate_symbols(self) -> Sequence[str]:
        return ()


_CONFORMS: ExchangeProvider = ConformingProvider()
'''

BROKEN_PROVIDER: Final = '''\
"""An exchange provider whose `fetch_fill_page` forgot the `symbol` argument.

Present, spelled correctly and a coroutine function: exactly what `isinstance` against a
runtime-checkable protocol would accept. Only a signature check rejects it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.base import (
    CursorKind,
    ExchangeCapabilities,
    ExchangeProvider,
    FillPage,
    FillWindow,
    RateLimit,
    assemble_fill_page,
)


class BrokenProvider:
    @property
    def capabilities(self) -> ExchangeCapabilities:
        return ExchangeCapabilities(
            exchange_key=ExchangeKey.BINGX,
            retention=None,
            max_query_window=timedelta(days=7),
            page_size=100,
            cursor_kind=CursorKind.NONE,
            rate_limit=RateLimit(max_requests=10, per_ms=1000),
            requires_symbol=False,
        )

    async def fetch_fill_page(self, window: FillWindow, *, cursor: str | None) -> FillPage:
        return assemble_fill_page(
            window,
            (),
            capabilities=self.capabilities,
            cursor=cursor,
            next_cursor=None,
            symbol=None,
        )

    async def candidate_symbols(self) -> Sequence[str]:
        return ()


_BROKEN: ExchangeProvider = BrokenProvider()
'''


def test_mypy_rejects_an_exchange_provider_with_the_wrong_signature(tmp_path: Path) -> None:
    conforming = tmp_path / "conforming_exchange_provider.py"
    broken = tmp_path / "broken_exchange_provider.py"
    conforming.write_text(CONFORMING_PROVIDER, encoding="utf-8")
    broken.write_text(BROKEN_PROVIDER, encoding="utf-8")

    result = run_mypy([conforming, broken], cache_dir=tmp_path / "mypy-cache")
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"mypy accepted a provider with the wrong signature:\n{output}"
    offending = [line for line in output.splitlines() if conforming.name in line]
    assert offending == [], f"the control file failed to type check:\n{chr(10).join(offending)}"
    assert broken.name in output, output
    assert "fetch_fill_page" in output, output


def test_the_fake_that_ships_with_the_suite_is_the_one_mypy_checks() -> None:
    source = (Path(__file__).parent / "fakes.py").read_text(encoding="utf-8")

    assert "_CONFORMS: ExchangeProvider = FakeExchangeProvider()" in source


# --------------------------------------------------------------------------------------
# Pagination through the protocol: empty, single, several, and stuck
# --------------------------------------------------------------------------------------


async def test_an_empty_venue_answers_one_empty_page() -> None:
    provider = FakeExchangeProvider({None: ((), None)})

    pages = await drain(provider)

    assert [len(page.fills) for page in pages] == [0]
    assert provider.calls == [(None, None)]


async def test_a_single_page_ends_the_walk() -> None:
    provider = FakeExchangeProvider({None: ((fill("1"), fill("2")), None)})

    pages = await drain(provider)

    assert [[f.external_trade_id for f in page.fills] for page in pages] == [["1", "2"]]


async def test_several_pages_are_followed_by_their_cursors_to_the_end() -> None:
    provider = FakeExchangeProvider(
        {
            None: ((fill("9"), fill("8"), fill("7")), "c7"),
            "c7": ((fill("6"), fill("5"), fill("4")), "c4"),
            "c4": ((fill("3"),), None),
        }
    )

    pages = await drain(provider)

    assert [f.external_trade_id for page in pages for f in page.fills] == [
        "9", "8", "7", "6", "5", "4", "3",
    ]  # fmt: skip
    assert provider.calls == [(None, None), ("c7", None), ("c4", None)]
    assert all(len(page.fills) <= FAKE_PAGE_SIZE for page in pages)


async def test_a_cursor_that_stops_advancing_fails_the_page_instead_of_looping() -> None:
    """The venue hands back the cursor it was given: the second page raises, legibly.

    The positive companion is the first page: it was served and had a cursor, so the
    refusal is about the repeat and not about the script.
    """
    provider = FakeExchangeProvider(
        {
            None: ((fill("9"), fill("8")), "c8"),
            "c8": ((fill("7"),), "c8"),
        }
    )

    with pytest.raises(ExchangeSchemaError) as caught:
        await drain(provider, max_pages=50)

    assert type(caught.value) is ExchangeSchemaError
    assert provider.calls == [(None, None), ("c8", None)]


async def test_an_overfull_page_is_refused_through_the_protocol() -> None:
    provider = FakeExchangeProvider(
        {None: (tuple(fill(str(number)) for number in range(FAKE_PAGE_SIZE + 1)), None)}
    )

    with pytest.raises(ExchangeSchemaError):
        await drain(provider)


async def test_a_venue_without_symbol_discovery_offers_no_candidates() -> None:
    assert await FakeExchangeProvider().candidate_symbols() == ()
    assert await FakeExchangeProvider(symbols=["BTCUSDT"]).candidate_symbols() == ("BTCUSDT",)
