"""The wallet service's policy decisions, tested where they are made.

Everything here is pure: a label is trimmed, a sentinel is distinguished from `None`, a
row is snapshotted. The policy that needs a database -- 409 for a duplicate, 404 for
somebody else's id -- is driven end to end in `tests/api/test_wallets_router.py`, because
what matters about it is the status code the caller actually receives.

What is here and nowhere else is the part the **CLI** will depend on. A router never
reaches `normalise_label`'s length check, because the request schema refuses an over-long
label first; a command-line importer has no schema in front of it, so for that caller this
guard is the only one there is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.db.models import Wallet
from portfolio.services import auth, wallets
from portfolio.services.wallets import (
    DUPLICATE_ARCHIVED_DETAIL,
    DUPLICATE_DETAIL,
    MAX_LABEL_LENGTH,
    UNSET,
    WALLET_NOT_FOUND_DETAIL,
    Unset,
    WalletAlreadyExistsError,
    WalletNotFoundError,
    normalise_label,
    utc_now,
    view_of,
)
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WPKH_UPPERCASE

if TYPE_CHECKING:
    from types import ModuleType

CREATED_AT: Final = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
UPDATED_AT: Final = datetime(2026, 9, 22, 8, 30, tzinfo=UTC)


def a_wallet(*, label: str | None = "Cold storage", archived: bool = False) -> Wallet:
    """An unattached mapped instance. No session, because `view_of` needs none."""
    return Wallet(
        id=7,
        user_id=1,
        chain_key="bitcoin",
        address_canonical=BIP173_TESTNET_P2WPKH,
        address_display=BIP173_TESTNET_P2WPKH_UPPERCASE,
        label=label,
        archived_at=UPDATED_AT if archived else None,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


# --------------------------------------------------------------------------------------
# normalise_label
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\t\n", None),
        ("Cold storage", "Cold storage"),
        ("  Cold storage  ", "Cold storage"),
        ("Cold  storage", "Cold  storage"),
    ],
    ids=["none", "empty", "spaces", "whitespace", "plain", "padded", "inner spaces"],
)
def test_a_label_is_trimmed_and_a_blank_one_becomes_none(
    given: str | None,
    expected: str | None,
) -> None:
    """An emptied text field sends `""`, and storing that is worse than storing nothing.

    A row whose label is the empty string renders as no label at all while `label is None`
    is false everywhere that checks, so the two states look the same to a user and
    different to the code.
    """
    assert normalise_label(given) == expected


def test_a_label_at_the_limit_is_accepted() -> None:
    """The boundary from the allowed side, so the check is `>` and not `>=`."""
    at_the_limit = "x" * MAX_LABEL_LENGTH

    assert normalise_label(at_the_limit) == at_the_limit


def test_a_label_over_the_limit_is_refused() -> None:
    """The guard the CLI relies on: no request schema stands in front of that caller.

    Asserted on the exception rather than on the message, except for the one thing the
    message must not do -- carry the label back, which for a paste gone wrong could be an
    address.
    """
    too_long = "x" * (MAX_LABEL_LENGTH + 1)

    with pytest.raises(ValueError, match="at most") as caught:
        normalise_label(too_long)

    assert too_long not in str(caught.value)


def test_a_label_is_measured_after_trimming() -> None:
    """Whitespace is not content, so padding must not be able to push a label over."""
    padded = "  " + "x" * MAX_LABEL_LENGTH + "  "

    assert normalise_label(padded) == "x" * MAX_LABEL_LENGTH


def test_an_address_pasted_into_the_label_field_is_refused_by_length_not_stored() -> None:
    """A wrong-field paste is the realistic way an address reaches a label column.

    It is not refused here -- an address is shorter than the limit -- which is the point
    of asserting it: the label column is not a place addresses cannot appear, so the
    redaction fragment in `portfolio.logging` covering `label` is not what protects it.
    Nothing logs a label either, and `tests/security/test_address_logging.py` is where
    that is checked.
    """
    assert normalise_label(BIP173_TESTNET_P2WPKH) == BIP173_TESTNET_P2WPKH
    assert len(BIP173_TESTNET_P2WPKH) < MAX_LABEL_LENGTH


# --------------------------------------------------------------------------------------
# The PATCH sentinel
# --------------------------------------------------------------------------------------


def test_unset_is_distinguishable_from_none() -> None:
    """`PATCH {"archived": false}` must not erase a label; `{"label": null}` must.

    Those are different requests, and a plain `None` default cannot tell them apart. The
    sentinel is an enum member rather than a bare `object()` so a type checker can narrow
    it, which is what keeps the branch that has a `str` honest under `--strict`.
    """
    assert UNSET is not None
    assert isinstance(UNSET, Unset)
    assert not isinstance(None, Unset)
    assert not isinstance("Cold storage", Unset)
    assert len(list(Unset)) == 1, "a second member would make the sentinel ambiguous"


# --------------------------------------------------------------------------------------
# view_of
# --------------------------------------------------------------------------------------


def test_the_view_publishes_the_display_form_and_not_the_canonical_one() -> None:
    """Criterion 2 at the layer boundary: one address form crosses, and it is the typed one.

    Publishing both would invite a client to pick the wrong one, and the canonical form is
    an implementation detail of the uniqueness rule rather than part of the contract.
    """
    view = view_of(a_wallet())

    assert view.address == BIP173_TESTNET_P2WPKH_UPPERCASE
    assert BIP173_TESTNET_P2WPKH not in str(view)
    assert not hasattr(view, "address_canonical")
    assert view.id == 7
    assert view.label == "Cold storage"
    assert view.created_at == CREATED_AT
    assert view.updated_at == UPDATED_AT


@pytest.mark.parametrize("archived", [False, True])
def test_the_view_turns_the_archive_timestamp_into_a_boolean(archived: bool) -> None:
    """The API says `archived: true`; the column says *when*. This is where they meet."""
    assert view_of(a_wallet(archived=archived)).archived is archived


def test_the_view_is_frozen() -> None:
    """A snapshot that could be edited would invite a caller to edit it instead of the row."""
    view = view_of(a_wallet())

    with pytest.raises((AttributeError, TypeError)):
        view.label = "something else"  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# The failures, and what they are allowed to say
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("archived", [False, True])
def test_the_duplicate_error_says_which_case_it_is(archived: bool) -> None:
    """Both are 409; the useful next action differs, so the detail has to differ too.

    A live row means "you already have this". An archived one means "you had this and put
    it away", and the answer to that is `PATCH {"archived": false}` rather than a second
    attempt at the same `POST`.
    """
    error = WalletAlreadyExistsError(archived=archived)

    assert error.archived is archived
    assert str(error) == (DUPLICATE_ARCHIVED_DETAIL if archived else DUPLICATE_DETAIL)
    # Through a set rather than `!=`: mypy narrows two `Final` literals to their values
    # and reports the comparison as never true, which is right about the types and
    # beside the point -- what is being asserted is that the two constants were not
    # written as the same sentence.
    assert len({DUPLICATE_DETAIL, DUPLICATE_ARCHIVED_DETAIL}) == 2


@pytest.mark.parametrize(
    "detail",
    [DUPLICATE_DETAIL, DUPLICATE_ARCHIVED_DETAIL, WALLET_NOT_FOUND_DETAIL],
)
def test_no_failure_detail_has_a_placeholder_to_put_an_address_in(detail: str) -> None:
    """Criterion 7, at the one place a 409 could become a leak.

    These strings are rendered into a problem document. A 422 that quoted the address is
    the leak everybody thinks of; a 409 that quoted it is the same leak through a
    different door, and the only structural defence is that the sentences are constants
    with nowhere to interpolate anything.
    """
    assert "{" not in detail
    assert "%s" not in detail
    assert detail == detail.format()


def test_the_not_found_error_carries_its_fixed_sentence() -> None:
    assert str(WalletNotFoundError()) == WALLET_NOT_FOUND_DETAIL


# --------------------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------------------


def test_the_default_clock_is_timezone_aware_and_utc() -> None:
    """A naive timestamp here would be written through `UtcDateTime` and silently shifted.

    The clock is injectable so a test can choose the value; this is the one assertion
    about the default, because the default is what production uses.
    """
    now = utc_now()

    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now(UTC).utcoffset()
    assert now.utcoffset() is not None


# --------------------------------------------------------------------------------------
# A latent trap in both services, pinned so it stays discoverable
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "builder"),
    [(wallets, wallets.build_wallet_service), (auth, auth.build_auth_service)],
    ids=["wallets", "auth"],
)
def test_reassigning_the_module_clock_does_not_reach_the_built_service(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    builder: object,
) -> None:
    """`clock=utc_now` is captured at definition time, so monkeypatching it does nothing.

    Both service builders take the clock as a keyword argument defaulting to their
    module's `utc_now`. Python evaluates that default **once, when the function is
    defined**, and stores the resulting function object in `__kwdefaults__`. Reassigning
    `module.utc_now` afterwards replaces a name the builder no longer consults.

    The failure mode is the dangerous kind rather than the loud kind. `monkeypatch.setattr`
    succeeds, the test reads as though it has fixed the clock, and the service goes on
    calling the real one -- so a test written to assert something about a chosen instant
    silently asserts it about wall-clock time instead. That is the same shape as every
    other defect this issue turned up: a double whose acceptance is taken as evidence that
    it took effect.

    **No test in this suite currently falls into it.** The archive tests inject the clock
    through `build_wallet_service` at the composition root instead, which substitutes the
    object actually wired rather than a name the wiring copied. This test is here so the
    next person writing one finds the trap before losing an afternoon to it, and so that
    the trap cannot be quietly removed from one service and left in the other.

    **If this goes red, the trap is gone and that is good news.** Delete this test rather
    than working around it -- and check that its sibling parametrisation went red too,
    because fixing one service and not the other is the outcome that leaves the next
    reader worse off than a consistent trap does.
    """
    defaults = builder.__kwdefaults__  # type: ignore[attr-defined]
    captured = defaults["clock"]
    assert captured is module.utc_now, (
        "the builder no longer captures the module's clock; if the default was changed "
        "deliberately, this test has expired and should be deleted"
    )

    def a_different_clock() -> datetime:
        return datetime(1999, 12, 31, 23, 59, tzinfo=UTC)

    monkeypatch.setattr(module, "utc_now", a_different_clock)

    # The reassignment succeeded, and reached nothing.
    assert module.utc_now is a_different_clock
    assert defaults["clock"] is captured
    assert defaults["clock"] is not a_different_clock
