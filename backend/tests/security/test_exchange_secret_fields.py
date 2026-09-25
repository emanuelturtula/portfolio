"""Criterion 6: no model #12 adds stores secret material, and the scan that says so can fail.

"Model" is read as the spec reads it: both ORM tables this issue adds, and every dataclass a
provider returns -- `NormalizedFill`, `FillPage`, `ExchangeCapabilities`, `RateLimit`,
`FillWindow`, `RetentionClamp`. `Credentials` is the one type whose whole purpose is to hold
secrets, and it is excluded by name; the companion tests use it as the positive control,
because a scan that does not flag `Credentials` is a scan that flags nothing.

Two independent checks, because they fail differently. **By name**, through
`portfolio.logging.is_sensitive_key` -- the same predicate the log redactor uses, so a field
this passes is one the redactor would also render. **By type**, over the declared
annotations, so a field innocently named `material` but typed `SecretStr` is still caught.

Rule 3: credentials are read from the environment, never persisted. A column that could hold
one is how that rule gets broken in a diff nobody reads twice.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final

from sqlalchemy import Column, MetaData, Table, Text

from portfolio.db.models import ExchangeAccount, ExchangeFill, metadata
from portfolio.logging import is_sensitive_key
from portfolio.providers.exchanges.base import (
    ExchangeCapabilities,
    FillPage,
    FillWindow,
    NormalizedFill,
    RateLimit,
    RetentionClamp,
)
from portfolio.providers.exchanges.credentials import Credentials

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

EXCHANGE_TABLES: Final = ("exchange_accounts", "exchange_fills")
MAPPED_CLASSES: Final = (ExchangeAccount, ExchangeFill)
PROVIDER_DATACLASSES: Final = (
    NormalizedFill,
    FillPage,
    ExchangeCapabilities,
    RateLimit,
    FillWindow,
    RetentionClamp,
)

#: The spellings of a secret-bearing type in an annotation. Annotations are strings under
#: `from __future__ import annotations`, so this reads them as text rather than resolving
#: them -- resolution would need every `TYPE_CHECKING`-only import to be importable.
SECRET_TYPE_NAMES: Final = ("SecretStr", "SecretBytes", "Credentials")

#: A handful of names the scan must have visited, so it cannot pass by visiting nothing.
MUST_BE_SCANNED: Final = frozenset(
    {
        "exchange_accounts.exchange_key",
        "exchange_fills.external_trade_id",
        "exchange_fills.raw_payload",
        "NormalizedFill.raw_payload",
        "ExchangeCapabilities.rate_limit",
        "RateLimit.max_requests",
        "FillWindow.since",
        "RetentionClamp.effective_since",
        "FillPage.next_cursor",
    }
)


def table_columns(tables: Iterable[Table]) -> dict[str, str]:
    return {
        f"{table.name}.{column.name}": column.name for table in tables for column in table.columns
    }


def dataclass_fields(classes: Iterable[type]) -> dict[str, str]:
    return {
        f"{cls.__name__}.{field.name}": field.name
        for cls in classes
        for field in dataclasses.fields(cls)
    }


def flagged_by_name(fields: Mapping[str, str]) -> list[str]:
    return sorted(qualified for qualified, name in fields.items() if is_sensitive_key(name))


def annotation_texts(classes: Iterable[type]) -> dict[str, str]:
    """Every declared annotation, as text, for dataclasses and mapped classes alike."""
    texts: dict[str, str] = {}
    for cls in classes:
        for name, annotation in vars(cls).get("__annotations__", {}).items():
            texts[f"{cls.__name__}.{name}"] = str(annotation)
    return texts


def flagged_by_type(texts: Mapping[str, str]) -> list[str]:
    return sorted(
        qualified
        for qualified, text in texts.items()
        if any(secret in text for secret in SECRET_TYPE_NAMES)
    )


def scanned_fields() -> dict[str, str]:
    tables = [metadata.tables[name] for name in EXCHANGE_TABLES]
    return {**table_columns(tables), **dataclass_fields(PROVIDER_DATACLASSES)}


def test_no_exchange_model_field_is_named_like_a_secret() -> None:
    fields = scanned_fields()

    assert flagged_by_name(fields) == []
    # The positive companion: the scan visited real fields, including the ones named here.
    assert len(fields) > 20, sorted(fields)
    assert fields.keys() >= MUST_BE_SCANNED, sorted(MUST_BE_SCANNED - fields.keys())


def test_the_field_scan_would_catch_a_secret_field() -> None:
    """A planted `api_secret` field and a planted `api_key` column are both flagged."""

    @dataclasses.dataclass(frozen=True)
    class PlantedFill:
        external_trade_id: str
        api_secret: str

    planted_table = Table(
        "planted_accounts",
        MetaData(),
        Column("id", Text, primary_key=True),
        Column("api_key", Text),
        Column("passphrase", Text),
    )

    assert flagged_by_name(dataclass_fields([PlantedFill])) == ["PlantedFill.api_secret"]
    assert flagged_by_name(table_columns([planted_table])) == [
        "planted_accounts.api_key",
        "planted_accounts.passphrase",
    ]
    # And the excluded type itself would be caught, which is why it is excluded by name.
    assert flagged_by_name(dataclass_fields([Credentials])) == [
        "Credentials.api_key",
        "Credentials.api_secret",
        "Credentials.passphrase",
    ]


def test_no_exchange_model_field_is_typed_as_a_secret() -> None:
    texts = annotation_texts([*PROVIDER_DATACLASSES, *MAPPED_CLASSES])

    assert flagged_by_type(texts) == []
    # The positive companion: the annotations were read, from both kinds of class.
    assert "NormalizedFill.quantity" in texts
    assert "Decimal" in texts["NormalizedFill.quantity"]
    assert "ExchangeFill.external_trade_id" in texts
    assert "Mapped" in texts["ExchangeFill.external_trade_id"]


def test_the_type_scan_would_catch_a_secret_typed_field() -> None:
    texts = annotation_texts([Credentials])

    assert flagged_by_type(texts) == [
        "Credentials.api_key",
        "Credentials.api_secret",
        "Credentials.passphrase",
    ]


def test_no_exchange_table_is_missing_from_the_scan() -> None:
    """Every table whose name starts `exchange_` is scanned, so a third one cannot slip past."""
    assert {name for name in metadata.tables if name.startswith("exchange_")} == set(
        EXCHANGE_TABLES
    )
    assert {cls.__tablename__ for cls in MAPPED_CLASSES} == set(EXCHANGE_TABLES)
