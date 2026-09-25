"""The fake exchange provider, and the one line that is the protocol's static check.

`_CONFORMS: ExchangeProvider = FakeExchangeProvider()` at module level is the whole of the
conformance check, exactly as `tests/providers/fakes.py` does it for chains: the gate's
`mypy --strict` over `tests` decides whether the assignment type checks, so a member that is
missing, takes the wrong arguments, or is a plain `def` where the protocol says `async def`
fails the gate. `ExchangeProvider` is not `@runtime_checkable`, for the reason `ChainProvider`
is not: `isinstance` would compare names and nothing about their signatures.

The fake answers from a script of pages keyed by the cursor that asks for them, and builds
every page through `assemble_fill_page` -- so a script that repeats a cursor, overfills a
page or answers outside the window is refused by the same code a real venue's parser uses.
It lives in `tests/` so the production image never ships a fake venue.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.base import (
    CursorKind,
    ExchangeCapabilities,
    ExchangeProvider,
    RateLimit,
    assemble_fill_page,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.providers.exchanges.base import FillPage, FillWindow, NormalizedFill

#: Small, so a test can fill a page with three fills and overfill it with four.
FAKE_PAGE_SIZE: Final = 3

type PageScript = Mapping[str | None, tuple[Sequence[NormalizedFill], str | None]]


class FakeExchangeProvider:
    """A venue that answers from a script and never opens a socket.

    `pages` maps the cursor a request carries -- `None` for the first page -- to the fills
    that page holds and the cursor the venue hands back. `calls` records every request in
    order, so a test asserts on what the provider was actually asked.
    """

    def __init__(
        self,
        pages: PageScript | None = None,
        *,
        requires_symbol: bool = False,
        symbols: Sequence[str] = (),
    ) -> None:
        self._pages: dict[str | None, tuple[Sequence[NormalizedFill], str | None]] = dict(
            pages or {None: ((), None)}
        )
        self._symbols = tuple(symbols)
        self.calls: list[tuple[str | None, str | None]] = []
        self._capabilities = ExchangeCapabilities(
            exchange_key=ExchangeKey.BITGET,
            retention=timedelta(days=90),
            max_query_window=timedelta(days=7),
            page_size=FAKE_PAGE_SIZE,
            cursor_kind=CursorKind.TRADE_ID_BEFORE,
            rate_limit=RateLimit(max_requests=10, per_ms=1000),
            requires_symbol=requires_symbol,
        )

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return self._capabilities

    async def fetch_fill_page(
        self, window: FillWindow, *, cursor: str | None, symbol: str | None
    ) -> FillPage:
        self.calls.append((cursor, symbol))
        fills, next_cursor = self._pages[cursor]
        return assemble_fill_page(
            window,
            fills,
            capabilities=self._capabilities,
            cursor=cursor,
            next_cursor=next_cursor,
            symbol=symbol,
        )

    async def candidate_symbols(self) -> Sequence[str]:
        return self._symbols


_CONFORMS: ExchangeProvider = FakeExchangeProvider()
"""The static check. Do not delete, and do not replace with an `isinstance` assertion."""
