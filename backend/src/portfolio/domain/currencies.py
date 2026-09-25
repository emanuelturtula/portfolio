"""The fiat currencies a holding may be valued in.

**This is the third spelling of the same two words, and each copy has a reason to exist.**

* `providers/prices/base.py` has `USD` and `EUR`, because a source builds vendor pair codes
  out of them. It could import these and does not need to.
* `db/models.py` has `_PRICE_QUOTE_CURRENCY_CHECK`, because a `CHECK` is SQL text, and the
  migration carrying its twin is a historical record that must not import anything live.
* This module has the enumeration the read path validates against.

The read path is the one that forced a new copy. `GET /api/balances/current` has to refuse a
currency nothing prices, and the router that does it may not import `providers.prices` --
`backend/.importlinter`'s `prices-are-never-fetched-in-a-request` contract forbids that edge
with no `allow_indirect_imports`, deliberately. `ChainKey.asset_symbol` moved into `domain`
for exactly the same reason; this is its sibling.

A test holds the three together, the way the reflection tests hold each `CHECK` constant to
its migration. Adding a currency is therefore a migration, a source that quotes it, and a
member here -- and the test fails until all three agree.

## Why refusing matters

Before this, `?quote_currency=gbp` answered `200` with a total of zero and every holding
unpriced for `reason: never_fetched`. That reason sends an operator to check whether the
refresh has run, when the real cause is that nothing will ever price that currency. A 422
naming the two accepted values is the answer that points at the actual mistake.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["QuoteCurrency"]


class QuoteCurrency(StrEnum):
    """A currency holdings may be valued in. The member is its own wire form.

    **Case-sensitive, and deliberately.** ISO 4217 codes are upper case, `prices` stores them
    upper case, and an enumeration in the OpenAPI document means the generated client can
    only send one of these two strings. Accepting `eur` as well would be a second spelling
    the database never holds, normalised in a router that is supposed to only parse.

    **Neither is ever derived from the other.** Valuing a EUR portfolio from a USD price and
    a cross rate would put a second vendor's error into every number with nothing saying so;
    a holding with only a USD price is unpriced in EUR, which is #9's contract.
    """

    EUR = "EUR"
    USD = "USD"
