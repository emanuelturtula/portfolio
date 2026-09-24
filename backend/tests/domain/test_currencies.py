"""`QuoteCurrency`, and the two other places the same two words are written down.

Review made `quote_currency` an enum so that a currency nothing prices is a 422 rather than a
page of `never_fetched`. That put the list of currencies in a third place:

| Where | Why it cannot import the others |
|---|---|
| `domain.currencies.QuoteCurrency` | the request path may not import `providers.prices` |
| `providers.prices.base.EUR` / `USD` | a price source builds vendor codes out of them |
| `db.models._PRICE_QUOTE_CURRENCY_CHECK` | SQL text, duplicated into its migration |

Three copies of one fact, held together the way `_ASSET_KIND_CHECK` and its migration are:
by a test that fails the moment one of them moves without the others. The failure it
prevents is a currency that validates at the API, has no price source, and cannot be stored
-- or the reverse, a stored currency the API refuses to show.
"""

from __future__ import annotations

import re
from typing import Final

from portfolio.db.models import _PRICE_QUOTE_CURRENCY_CHECK
from portfolio.domain.currencies import QuoteCurrency
from portfolio.providers.prices.base import EUR, SUPPORTED_PAIRS, USD

#: A quoted SQL string literal. Deliberately simple: the constant is one `IN (...)` list.
SQL_LITERAL: Final = re.compile(r"'([^']*)'")


def admitted_by(check: str) -> set[str]:
    """The literals an `IN ('A', 'B')` check admits."""
    return set(SQL_LITERAL.findall(check))


def test_the_enum_the_price_constants_and_the_column_check_name_the_same_currencies() -> None:
    """One set, three spellings, and every pair this product prices uses one of them."""
    enum_values = {member.value for member in QuoteCurrency}

    assert enum_values == {EUR, USD}
    assert enum_values == admitted_by(_PRICE_QUOTE_CURRENCY_CHECK)
    assert {currency for _symbol, currency in SUPPORTED_PAIRS} == enum_values


def test_the_member_is_its_own_wire_form() -> None:
    """A `StrEnum`, so the value that crosses the API is the value in the column."""
    assert QuoteCurrency("EUR") is QuoteCurrency.EUR
    assert str(QuoteCurrency.USD) == "USD"


def test_the_check_parser_can_actually_see_a_difference() -> None:
    """The control: a parser that found nothing would make the first test pass on `set()`."""
    assert admitted_by("quote_currency IN ('EUR', 'USD')") == {"EUR", "USD"}
    assert admitted_by("quote_currency IN ('EUR', 'USD', 'GBP')") != {EUR, USD}
    assert admitted_by(_PRICE_QUOTE_CURRENCY_CHECK) != set()
