"""Declared conversion in the ledger: posting, replay, blocks, closing (design 8.3, 8.4).

The fixture tests/vectors/conversion_ledger_fixture_v1.json uses two existing ledger
currencies as stand-ins and a rate of 9 that is not a real rate: the ledger's currencies are
a closed set, so a ledger-level test cannot use a fictional code.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.conversion import (
    CLOSING_METADATA_KEY,
    CLOSING_PURPOSE_CODE,
    CONVERSION_METADATA_KEY,
    CONVERSION_PURPOSE_CODE,
    ConversionBatchResult,
    ConversionBlockedError,
    ConversionError,
    ConversionIncompleteError,
    ConversionReplayError,
    close_conversion,
    closing_transaction_id,
    conversion_transaction_id,
    load_declaration,
    post_conversion,
    post_conversion_batch,
    read_closing,
    read_posted_conversion,
)
from openreserve.core.exact import INSTANT_MAX, instant_from_text
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import (
    Account,
    Currency,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "vectors" / "conversion_ledger_fixture_v1.json").read_text(encoding="utf-8")
)
DECLARATION = load_declaration(FIXTURE["declaration"])
FUNDED_AT = instant_from_text(FIXTURE["funding_instant"])
CUTOFF = DECLARATION.cutoff
EFFECTIVE = instant_from_text(FIXTURE["effective_at"])
CLEARING_JPY, CLEARING_USD, RESIDUE = "acct-clearing-jpy", "acct-clearing-usd", "acct-residue-usd"
PAIRS = [tuple(pair) for pair in FIXTURE["conversion_pairs"]]
CURRENCIES = {"JPY": Currency.JPY, "USD": Currency.USD}


class ConversionLedger:
    """The ledger of the fixture: accounts with fixed ids, funded at the funding instant."""

    def __init__(self, fund: bool = True) -> None:
        self.storage = SQLiteLedgerStorage(":memory:")
        self.ledger = Ledger(self.storage)
        for account in FIXTURE["accounts"]:
            self.open(account["account_id"], OwnerType(account["account_type"]), CURRENCIES[account["currency"]], account["label"])
        if fund:
            for src, dst, amount in FIXTURE["funding_transfers_jpy"]:
                self.move(src, dst, int(amount), at=FUNDED_AT)

    def open(self, account_id, owner_type, currency, label=None, created_at=None) -> str:
        self.storage.save_account(
            Account(account_id=account_id, owner_type=owner_type, currency=currency, label=label or account_id,
                    created_at=created_at or FUNDED_AT)
        )
        return account_id

    def move(self, src, dst, amount, *, at, settle=True, currency=Currency.JPY) -> str:
        b = TransactionBuilder("MOVE", "test", initiated_at=at)
        b.transfer(src, dst, Money(cents=amount, currency=currency))
        self.ledger.post(b.build())
        if settle:
            self.ledger.settle(b.transaction_id, settled_at=at)
        return b.transaction_id

    def balance(self, account_id, at=INSTANT_MAX) -> int:
        return self.ledger.balance(account_id, as_of=at).cents

    def convert(self, source, target, **overrides):
        arguments = dict(
            source_account_id=source,
            target_account_id=target,
            clearing_source_account_id=CLEARING_JPY,
            clearing_target_account_id=CLEARING_USD,
            effective_at=EFFECTIVE,
            initiator_id="ops",
        )
        arguments.update(overrides)
        return post_conversion(self.ledger, DECLARATION, **arguments)

    def convert_all(self, pairs=PAIRS, **overrides) -> ConversionBatchResult:
        arguments = dict(
            pairs=pairs,
            clearing_source_account_id=CLEARING_JPY,
            clearing_target_account_id=CLEARING_USD,
            effective_at=EFFECTIVE,
            initiator_id="ops",
        )
        arguments.update(overrides)
        return post_conversion_batch(self.ledger, DECLARATION, **arguments)

    def close(self, **overrides) -> int:
        arguments = dict(
            clearing_source_account_id=CLEARING_JPY,
            clearing_target_account_id=CLEARING_USD,
            residue_account_id=RESIDUE,
            effective_at=EFFECTIVE,
            initiator_id="ops",
        )
        arguments.update(overrides)
        return close_conversion(self.ledger, DECLARATION, **arguments)

    def counts(self) -> tuple[int, int]:
        return (
            self.storage._conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            self.storage._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
        )


@pytest.fixture
def fx():
    f = ConversionLedger()
    yield f
    f.storage.close()


def entries_of(transaction: Transaction) -> list[list[str]]:
    return [[e.account_id, str(e.amount.cents), e.amount.currency.code] for e in transaction.entries]


# ---------------------------------------------------------------------------
# The fixture, end to end
# ---------------------------------------------------------------------------


def test_fixture_declaration_and_instants():
    assert DECLARATION.declaration_hash == FIXTURE["declaration_hash"]
    assert DECLARATION.factor == Fraction(100, 9)
    assert EFFECTIVE - CUTOFF == timedelta(seconds=1)
    assert len(PAIRS) == 7


def test_each_conversion_posts_the_entries_of_the_fixture(fx):
    before = {account["account_id"]: fx.balance(account["account_id"], CUTOFF) for account in FIXTURE["accounts"]}
    assert before["acct-alice"] == 300_000 and before["acct-dave"] == -60 and before["acct-platform-jpy"] == -1_474_940
    assert sum(v for k, v in before.items() if not k.endswith("usd")) == 0

    for source, target in PAIRS:
        expected_entries = FIXTURE["expected_conversion_entries"][source]
        expected_id = FIXTURE["expected_conversion_transaction_ids"][source]
        assert conversion_transaction_id(DECLARATION, source) == expected_id
        transaction = fx.convert(source, target)
        if expected_entries is None:  # erin has no balance: nothing is posted
            assert transaction is None
            with pytest.raises(IntegrityError):
                fx.ledger.get_transaction(expected_id)
            continue
        stored = fx.ledger.get_transaction(expected_id)
        assert stored == transaction
        assert entries_of(stored) == expected_entries
        assert [e.entry_id for e in stored.entries] == [f"{expected_id}:{i}" for i in range(4)]
        assert [e.sequence for e in stored.entries] == [0, 1, 2, 3]
        assert stored.purpose_code == CONVERSION_PURPOSE_CODE == "CURRENCY_CONVERSION"
        assert stored.status is TransactionStatus.SETTLED
        assert stored.initiated_at == stored.settled_at == EFFECTIVE
        assert stored.initiator_id == "ops"
        x, y = -int(expected_entries[0][1]), int(expected_entries[3][1])
        assert stored.metadata == (
            (
                CONVERSION_METADATA_KEY,
                '{"declaration_hash":"%s","source_account_id":"%s","source_amount":"%d",'
                '"target_account_id":"%s","target_amount":"%d"}' % (FIXTURE["declaration_hash"], source, x, target, y),
            ),
        )
        done = read_posted_conversion(stored, DECLARATION, clearing_source_account_id=CLEARING_JPY,
                                      clearing_target_account_id=CLEARING_USD)
        assert (done.source_account_id, done.source_amount, done.target_account_id, done.target_amount) == (source, x, target, y)

    # alice, as in the design text
    alice = fx.ledger.get_transaction(FIXTURE["expected_conversion_transaction_ids"]["acct-alice"])
    assert [e.amount.cents for e in alice.entries] == [-300_000, 300_000, -3_333_333, 3_333_333]

    # before closing: the source clearing account is back at zero, the target one holds +1
    assert fx.balance(CLEARING_JPY) == 0
    assert fx.balance(CLEARING_USD) == 1 == int(FIXTURE["expected_clearing_target_balance_before_closing"])
    assert fx.balance(RESIDUE) == 0

    # closing books the difference to the institution's residue account
    assert fx.close() == 1 == int(FIXTURE["expected_residue_account_change"])
    assert fx.balance(CLEARING_JPY) == 0 and fx.balance(CLEARING_USD) == 0 and fx.balance(RESIDUE) == 1
    closing = fx.ledger.get_transaction(FIXTURE["expected_closing_transaction_id"])
    assert closing.transaction_id == closing_transaction_id(DECLARATION)
    assert entries_of(closing) == [[CLEARING_USD, "-1", "USD"], [RESIDUE, "1", "USD"]]
    assert closing.purpose_code == CLOSING_PURPOSE_CODE == "CURRENCY_CONVERSION_CLOSING"
    assert closing.status is TransactionStatus.SETTLED and closing.settled_at == EFFECTIVE
    assert closing.metadata == (
        (CLOSING_METADATA_KEY, '{"declaration_hash":"%s","residue_account_change":"1"}' % FIXTURE["declaration_hash"]),
    )

    # every currency journal still balances, and the past is unchanged
    usd = [a["account_id"] for a in FIXTURE["accounts"] if a["currency"] == "USD"]
    jpy = [a["account_id"] for a in FIXTURE["accounts"] if a["currency"] == "JPY"]
    assert sum(fx.balance(a) for a in usd) == 0
    assert all(fx.balance(a) == 0 for a in jpy)
    assert {a: fx.balance(a, CUTOFF) for a in before} == before
    assert {a: fx.balance(a) for a in usd} == {
        "acct-platform-usd": -16_388_222, "acct-reserve-usd": 11_111_111, "acct-alice-usd": 3_333_333,
        "acct-bob-usd": 1_333_333, "acct-carol-usd": 611_111, "acct-dave-usd": -667, "acct-erin-usd": 0,
        CLEARING_USD: 0, RESIDUE: 1,
    }


def test_batch_posts_in_ascending_order_of_the_source_id_and_reports(fx):
    result = fx.convert_all(pairs=reversed(PAIRS))
    assert result == ConversionBatchResult(
        posted=("acct-alice", "acct-bob", "acct-carol", "acct-dave", "acct-platform-jpy", "acct-reserve-jpy"),
        skipped=(),
        zero_balance=("acct-erin",),
    )
    # insertion order of the conversion transactions is the ascending order of source ids
    rows = fx.storage._conn.execute(
        "SELECT transaction_id FROM transactions WHERE purpose_code = 'CURRENCY_CONVERSION' ORDER BY rowid"
    ).fetchall()
    assert [r[0] for r in rows] == [FIXTURE["expected_conversion_transaction_ids"][s] for s in result.posted]
    for source in result.posted:
        stored = fx.ledger.get_transaction(FIXTURE["expected_conversion_transaction_ids"][source])
        assert entries_of(stored) == FIXTURE["expected_conversion_entries"][source]
    assert fx.balance(CLEARING_JPY) == 0 and fx.balance(CLEARING_USD) == 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_same_pair_again_and_same_source_with_a_fresh_target_are_replays(fx):
    fx.convert("acct-alice", "acct-alice-usd")
    counts = fx.counts()
    with pytest.raises(ConversionReplayError):
        fx.convert("acct-alice", "acct-alice-usd")
    fx.open("acct-alice-usd-2", OwnerType.USER, Currency.USD)
    with pytest.raises(ConversionReplayError):
        fx.convert("acct-alice", "acct-alice-usd-2")
    assert fx.counts() == counts
    assert fx.balance("acct-alice-usd") == 3_333_333 and fx.balance("acct-alice-usd-2") == 0
    assert issubclass(ConversionReplayError, ConversionError)


def test_hand_made_transaction_with_the_derived_id_is_refused_by_the_store(fx):
    transaction = fx.convert("acct-alice", "acct-alice-usd")
    counts = fx.counts()
    forged = Transaction(
        transaction_id=transaction.transaction_id,
        entries=(
            Entry(f"{transaction.transaction_id}:x0", transaction.transaction_id, "acct-clearing-usd", Money(-5, Currency.USD), 0),
            Entry(f"{transaction.transaction_id}:x1", transaction.transaction_id, "acct-alice-usd", Money(5, Currency.USD), 1),
        ),
        purpose_code="CURRENCY_CONVERSION",
        initiator_id="mallory",
        initiated_at=EFFECTIVE,
        settled_at=EFFECTIVE,
        status=TransactionStatus.SETTLED,
        external_refs=(),
        compliance_decision=None,
        metadata=(),
    )
    with pytest.raises(IntegrityError):
        fx.ledger.post(forged)
    with pytest.raises(IntegrityError):
        fx.storage.save_transaction(forged)
    assert fx.counts() == counts
    assert fx.balance("acct-alice-usd") == 3_333_333


def test_running_the_batch_again_posts_nothing(fx):
    first = fx.convert_all()
    counts = fx.counts()
    second = fx.convert_all()
    assert second == ConversionBatchResult(posted=(), skipped=first.posted, zero_balance=("acct-erin",))
    assert fx.counts() == counts
    # also after closing
    fx.close()
    counts = fx.counts()
    assert fx.convert_all().posted == ()
    assert fx.counts() == counts


def test_batch_resumes_after_an_interruption(fx):
    fx.convert("acct-alice", "acct-alice-usd")
    fx.convert("acct-carol", "acct-carol-usd")
    result = fx.convert_all()
    assert result.skipped == ("acct-alice", "acct-carol")
    assert result.posted == ("acct-bob", "acct-dave", "acct-platform-jpy", "acct-reserve-jpy")
    assert result.zero_balance == ("acct-erin",)
    assert fx.close() == 1


def test_batch_with_another_target_for_a_converted_source_is_a_replay(fx):
    fx.convert("acct-alice", "acct-alice-usd")
    fx.open("acct-alice-usd-2", OwnerType.USER, Currency.USD)
    counts = fx.counts()
    with pytest.raises(ConversionReplayError):
        fx.convert_all(pairs=[("acct-alice", "acct-alice-usd-2")])
    assert fx.counts() == counts


def test_batch_refuses_duplicate_sources_and_targets_before_posting(fx):
    counts = fx.counts()
    for pairs in (
        [("acct-alice", "acct-alice-usd"), ("acct-alice", "acct-bob-usd")],
        [("acct-alice", "acct-alice-usd"), ("acct-bob", "acct-alice-usd")],
        [("acct-alice",)],
        ["acct-alice"],
        [("acct-alice", 5)],
    ):
        with pytest.raises(ConversionError):
            fx.convert_all(pairs=pairs)
    assert fx.counts() == counts


def test_two_sources_cannot_be_converted_into_one_target(fx):
    fx.convert("acct-alice", "acct-alice-usd")
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-bob", "acct-alice-usd")
    assert fx.balance("acct-alice-usd") == 3_333_333 and fx.balance("acct-bob") == 120_000


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def test_pending_transaction_on_the_source_blocks(fx):
    pending = fx.move("acct-platform-jpy", "acct-alice", 5, at=FUNDED_AT, settle=False)
    counts = fx.counts()
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-alice", "acct-alice-usd")
    assert fx.counts() == counts
    # also a pending transaction that starts in the future
    fx.move("acct-platform-jpy", "acct-bob", 5, at=EFFECTIVE + timedelta(days=30), settle=False)
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-bob", "acct-bob-usd")
    # once it has failed, the conversion goes through with the settled balance
    fx.ledger.fail_transaction(pending, "cancelled")
    assert [e.amount.cents for e in fx.convert("acct-alice", "acct-alice-usd").entries] == [-300_000, 300_000, -3_333_333, 3_333_333]


def test_target_with_an_entry_blocks(fx):
    fx.move("acct-platform-usd", "acct-alice-usd", 1, at=FUNDED_AT, currency=Currency.USD)
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-alice", "acct-alice-usd")
    fx.move("acct-platform-usd", "acct-bob-usd", 1, at=EFFECTIVE + timedelta(days=1), settle=False, currency=Currency.USD)
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-bob", "acct-bob-usd")  # a pending entry on the target blocks as well
    assert fx.balance("acct-alice") == 300_000 and fx.balance("acct-bob") == 120_000


def test_settled_entry_on_the_source_after_the_cutoff_blocks(fx):
    fx.move("acct-platform-jpy", "acct-alice", 5, at=CUTOFF + timedelta(microseconds=1))
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-alice", "acct-alice-usd")
    # two later entries that cancel each other block too: something happened after the cut-off
    fx.move("acct-platform-jpy", "acct-bob", 7, at=CUTOFF + timedelta(microseconds=1))
    fx.move("acct-bob", "acct-platform-jpy", 7, at=CUTOFF + timedelta(microseconds=2))
    assert fx.balance("acct-bob") == fx.balance("acct-bob", CUTOFF) == 120_000
    with pytest.raises(ConversionBlockedError):
        fx.convert("acct-bob", "acct-bob-usd")
    # an entry settled exactly at the cut-off is part of the converted balance
    fx.move("acct-platform-jpy", "acct-carol", 9, at=CUTOFF)
    assert [e.amount.cents for e in fx.convert("acct-carol", "acct-carol-usd").entries][:2] == [-55_009, 55_009]


def test_effective_instant_must_be_aware_and_after_the_cutoff(fx):
    counts = fx.counts()
    for effective_at in (CUTOFF, CUTOFF - timedelta(seconds=1), FUNDED_AT, datetime(2026, 7, 1), None, "2026-06-30T15:00:01.000000Z"):
        with pytest.raises(ConversionError):
            fx.convert("acct-alice", "acct-alice-usd", effective_at=effective_at)
        with pytest.raises(ConversionError):
            fx.convert_all(effective_at=effective_at)
    assert fx.counts() == counts
    # one microsecond after the cut-off, in another offset, is fine
    later = (CUTOFF + timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=9)))
    assert fx.convert("acct-alice", "acct-alice-usd", effective_at=later).settled_at == CUTOFF + timedelta(microseconds=1)


def test_wrong_currencies_types_and_accounts_are_refused(fx):
    fx.open("acct-alice-eur", OwnerType.USER, Currency.EUR)
    fx.open("acct-clearing-eur", OwnerType.CLEARING, Currency.EUR)
    fx.open("acct-fee-usd", OwnerType.FEE, Currency.USD)
    fx.open("acct-usdc", OwnerType.USER, Currency.USDC)
    counts = fx.counts()
    cases = [
        dict(source="acct-alice", target="acct-alice-eur"),                                    # target in another currency
        dict(source="acct-alice-usd", target="acct-alice"),                                    # both swapped
        dict(source="acct-alice", target="acct-bob"),                                          # target in the source currency
        dict(source="acct-alice", target="acct-usdc"),
        dict(source="acct-alice", target="acct-fee-usd"),                                      # another account type
        dict(source="acct-alice", target="acct-reserve-usd"),
        dict(source="acct-alice", target="acct-alice-usd", clearing_source_account_id="acct-platform-jpy"),  # not CLEARING
        dict(source="acct-alice", target="acct-alice-usd", clearing_target_account_id="acct-residue-usd"),   # not CLEARING
        dict(source="acct-alice", target="acct-alice-usd", clearing_source_account_id=CLEARING_USD),         # wrong currency
        dict(source="acct-alice", target="acct-alice-usd", clearing_target_account_id="acct-clearing-eur"),
        dict(source="acct-alice", target="acct-alice-usd", clearing_target_account_id=CLEARING_JPY),
        dict(source="acct-nobody", target="acct-alice-usd"),                                   # unknown accounts
        dict(source="acct-alice", target="acct-nobody"),
        dict(source="acct-alice", target="acct-alice-usd", clearing_source_account_id="acct-nobody"),
        dict(source="acct-alice", target="acct-alice-usd", initiator_id=""),
        dict(source="acct-alice", target="acct-alice-usd", initiator_id=None),
        dict(source="", target="acct-alice-usd"),
        dict(source=None, target="acct-alice-usd"),
    ]
    for case in cases:
        source, target = case.pop("source"), case.pop("target")
        with pytest.raises(ConversionError) as info:
            fx.convert(source, target, **case)
        assert type(info.value) is ConversionError, (source, target, case)
    assert fx.counts() == counts


def test_source_in_another_currency_is_refused_also_when_the_target_is_right(fx):
    # Both accounts are in the target currency: the target is acceptable, the source is not.
    fx.move("acct-platform-usd", "acct-bob-usd", 700, at=FUNDED_AT, currency=Currency.USD)
    counts = fx.counts()
    for source in ("acct-bob-usd", "acct-carol-usd"):  # with and without a balance
        with pytest.raises(ConversionError) as info:
            fx.convert(source, "acct-alice-usd")
        assert type(info.value) is ConversionError
    assert fx.counts() == counts
    assert fx.balance("acct-bob-usd") == 700 and fx.balance("acct-alice-usd") == 0


def test_clearing_account_cannot_be_the_source_or_the_target(fx):
    fx.open("acct-clearing-jpy-2", OwnerType.CLEARING, Currency.JPY)
    fx.open("acct-clearing-usd-2", OwnerType.CLEARING, Currency.USD)
    with pytest.raises(ConversionError):
        fx.convert(CLEARING_JPY, "acct-clearing-usd-2")  # the source is its own clearing account
    with pytest.raises(ConversionError):
        fx.convert("acct-clearing-jpy-2", CLEARING_USD)  # the target is its own clearing account


def test_zero_balance_source_posts_nothing_and_can_be_asked_again(fx):
    counts = fx.counts()
    assert fx.convert("acct-erin", "acct-erin-usd") is None
    assert fx.convert("acct-erin", "acct-erin-usd") is None
    assert fx.counts() == counts


def test_amount_that_converts_to_zero_posts_only_the_source_leg():
    f = ConversionLedger(fund=False)
    # With the fixture's declaration (0 -> 2 minor units) no amount of at least 1 rounds to 0,
    # so this test declares a conversion from 6 to 0 minor units: stand-in currencies again.
    document = dict(FIXTURE["declaration"])
    document["source"] = {"code": "USDC", "minor_units": "6"}
    document["target"] = {"code": "JPY", "minor_units": "0"}
    declaration = load_declaration(document)  # factor 1/9000000
    assert declaration.factor.denominator == 9_000_000
    for account_id, owner_type, currency in (
        ("p-usdc", OwnerType.PLATFORM, Currency.USDC), ("small", OwnerType.USER, Currency.USDC),
        ("p-jpy", OwnerType.PLATFORM, Currency.JPY), ("small-jpy", OwnerType.USER, Currency.JPY),
        ("c-usdc", OwnerType.CLEARING, Currency.USDC), ("c-jpy", OwnerType.CLEARING, Currency.JPY),
        ("r-jpy", OwnerType.FX_GAIN_LOSS, Currency.JPY),
    ):
        f.open(account_id, owner_type, currency)
    f.move("p-usdc", "small", 4_000_000, at=FUNDED_AT, currency=Currency.USDC)  # converts to 0.444 -> 0
    common = dict(clearing_source_account_id="c-usdc", clearing_target_account_id="c-jpy", effective_at=EFFECTIVE, initiator_id="ops")
    tx = post_conversion(f.ledger, declaration, source_account_id="small", target_account_id="small-jpy", **common)
    assert [(e.account_id, e.amount.cents) for e in tx.entries] == [("small", -4_000_000), ("c-usdc", 4_000_000)]
    assert f.balance("small") == 0 and f.balance("small-jpy") == 0 and f.balance("c-jpy") == 0
    done = read_posted_conversion(tx, declaration, clearing_source_account_id="c-usdc", clearing_target_account_id="c-jpy")
    assert (done.source_amount, done.target_amount, done.target_account_id) == (4_000_000, 0, "small-jpy")
    # the platform side: -4000000 converts to 0 as well; then everything closes with nothing to book
    tx2 = post_conversion(f.ledger, declaration, source_account_id="p-usdc", target_account_id="p-jpy", **common)
    assert len(tx2.entries) == 2
    assert close_conversion(f.ledger, declaration, clearing_source_account_id="c-usdc", clearing_target_account_id="c-jpy",
                            residue_account_id="r-jpy", effective_at=EFFECTIVE, initiator_id="ops") == 0
    with pytest.raises(IntegrityError):
        f.ledger.get_transaction(closing_transaction_id(declaration))  # nothing to book, nothing posted
    # the replay protection does not depend on the target leg
    with pytest.raises(ConversionReplayError):
        post_conversion(f.ledger, declaration, source_account_id="small", target_account_id="small-jpy", **common)
    f.storage.close()


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------


def test_closing_needs_every_account_of_the_source_currency_converted(fx):
    fx.convert_all(pairs=[p for p in PAIRS if p[0] != "acct-carol"])
    counts = fx.counts()
    with pytest.raises(ConversionIncompleteError):
        fx.close()
    assert fx.counts() == counts
    fx.convert("acct-carol", "acct-carol-usd")
    assert fx.close() == 1
    assert issubclass(ConversionIncompleteError, ConversionError)


def test_closing_before_anything_is_converted_is_incomplete(fx):
    with pytest.raises(ConversionIncompleteError):
        fx.close()


def test_closing_twice_changes_nothing(fx):
    fx.convert_all()
    assert fx.close() == 1
    counts = fx.counts()
    assert fx.close() == 1
    assert fx.close() == 1
    assert fx.counts() == counts
    assert fx.balance(RESIDUE) == 1 and fx.balance(CLEARING_USD) == 0
    assert read_closing(fx.ledger, DECLARATION, clearing_source_account_id=CLEARING_JPY, clearing_target_account_id=CLEARING_USD,
                        residue_account_id=RESIDUE, effective_at=EFFECTIVE) == (1, True)


def test_read_closing_reports_an_open_conversion_without_posting(fx):
    fx.convert_all()
    counts = fx.counts()
    assert read_closing(fx.ledger, DECLARATION, clearing_source_account_id=CLEARING_JPY, clearing_target_account_id=CLEARING_USD,
                        residue_account_id=RESIDUE, effective_at=EFFECTIVE) == (1, False)
    assert fx.counts() == counts


def test_closing_refuses_a_shared_clearing_account(fx):
    fx.convert_all()
    fx.move(CLEARING_USD, "acct-platform-usd", 10, at=EFFECTIVE, currency=Currency.USD)  # something else in the clearing account
    with pytest.raises(ConversionError) as info:
        fx.close()
    assert type(info.value) is ConversionError
    assert fx.balance(RESIDUE) == 0


def test_closing_refuses_wrong_accounts_and_instants(fx):
    fx.open("acct-residue-jpy", OwnerType.FX_GAIN_LOSS, Currency.JPY)
    fx.open("acct-fee-usd", OwnerType.FEE, Currency.USD)
    fx.convert_all()
    counts = fx.counts()
    for overrides in (
        dict(residue_account_id="acct-residue-jpy"),      # wrong currency
        dict(residue_account_id="acct-fee-usd"),          # not FX_GAIN_LOSS
        dict(residue_account_id="acct-platform-usd"),
        dict(residue_account_id=CLEARING_USD),
        dict(residue_account_id="acct-nobody"),
        dict(clearing_target_account_id=RESIDUE),
        dict(clearing_source_account_id="acct-platform-jpy"),
        dict(effective_at=CUTOFF),
        dict(effective_at=datetime(2026, 7, 1)),
        dict(initiator_id=""),
    ):
        with pytest.raises(ConversionError) as info:
            fx.close(**overrides)
        assert type(info.value) is ConversionError, overrides
    # before the conversions' own instant the source balances are not zero yet
    with pytest.raises(ConversionIncompleteError):
        fx.close(effective_at=CUTOFF + timedelta(microseconds=1))
    assert fx.counts() == counts
    assert fx.close() == 1


def test_closing_with_another_residue_account_after_closing_is_refused(fx):
    fx.open("acct-residue-usd-2", OwnerType.FX_GAIN_LOSS, Currency.USD)
    fx.convert_all()
    fx.close()
    with pytest.raises(ConversionError):
        fx.close(residue_account_id="acct-residue-usd-2")
    assert fx.balance("acct-residue-usd-2") == 0 and fx.balance(RESIDUE) == 1


def test_negative_rounding_difference_is_borne_by_the_institution():
    # one holder with 5 (-> 55.56 -> 56) against the platform's -5 (-> -56): no difference;
    # two holders with 5 and 4 against -9 (-> -100): 56 + 44 - 100 = 0;
    # holders 1, 1, 1 (11 each = 33) against -3 (-33.33 -> -33): 0;
    # holders 5, 5 (56 + 56 = 112) against -10 (-111.11 -> -111): the clearing account holds -1.
    f = ConversionLedger(fund=False)
    f.move("acct-platform-jpy", "acct-alice", 5, at=FUNDED_AT)
    f.move("acct-platform-jpy", "acct-bob", 5, at=FUNDED_AT)
    f.convert_all()
    assert f.balance("acct-alice-usd") == 56 and f.balance("acct-bob-usd") == 56 and f.balance("acct-platform-usd") == -111
    assert f.balance(CLEARING_USD) == -1
    assert f.close() == -1  # the institution loses one minor unit to rounding
    assert f.balance(RESIDUE) == -1 and f.balance(CLEARING_USD) == 0
    closing = f.ledger.get_transaction(closing_transaction_id(DECLARATION))
    assert [(e.account_id, e.amount.cents) for e in closing.entries] == [(CLEARING_USD, 1), (RESIDUE, -1)]
    # no holder's amount was adjusted to make the totals agree
    assert f.balance("acct-alice-usd") == 56 and f.balance("acct-bob-usd") == 56
    f.storage.close()


def test_posted_conversion_reader_refuses_other_transactions(fx):
    transaction = fx.convert("acct-alice", "acct-alice-usd")
    common = dict(clearing_source_account_id=CLEARING_JPY, clearing_target_account_id=CLEARING_USD)
    assert read_posted_conversion(transaction, DECLARATION, **common).target_amount == 3_333_333
    other = fx.ledger.get_transaction(fx.move("acct-platform-jpy", "acct-erin", 1, at=FUNDED_AT))
    with pytest.raises(ConversionError):
        read_posted_conversion(other, DECLARATION, **common)
    with pytest.raises(ConversionError):  # read against another clearing account
        read_posted_conversion(transaction, DECLARATION, clearing_source_account_id=CLEARING_USD, clearing_target_account_id=CLEARING_USD)
    other_declaration = dict(FIXTURE["declaration"])
    other_declaration["declaration_id"] = "another"
    with pytest.raises(ConversionError):  # read under another declaration
        read_posted_conversion(transaction, other_declaration, **common)
    # the same content under an id that is not the derived one
    other_id = "conv_" + "0" * 64
    renamed = replace(
        transaction,
        transaction_id=other_id,
        entries=tuple(replace(e, transaction_id=other_id, entry_id=f"{other_id}:{e.sequence}") for e in transaction.entries),
    )
    with pytest.raises(ConversionError):
        read_posted_conversion(renamed, DECLARATION, **common)
    # the same content booked for another source account (the id is derived from the source)
    moved = replace(transaction, entries=(replace(transaction.entries[0], account_id="acct-bob"),) + transaction.entries[1:])
    with pytest.raises(ConversionError):
        read_posted_conversion(moved, DECLARATION, **common)
    tampered = replace(transaction, metadata=((CONVERSION_METADATA_KEY, transaction.metadata[0][1].replace("3333333", "3333334")),))
    with pytest.raises(ConversionError):
        read_posted_conversion(tampered, DECLARATION, **common)
    for bad_record in ("", "{}", "not json", '{"a":1}', "[" * 10000):
        with pytest.raises(ConversionError):
            read_posted_conversion(replace(transaction, metadata=((CONVERSION_METADATA_KEY, bad_record),)), DECLARATION, **common)
    with pytest.raises(ConversionError):
        read_posted_conversion(replace(transaction, metadata=()), DECLARATION, **common)
    with pytest.raises(ConversionError):
        read_posted_conversion(replace(transaction, status=TransactionStatus.PENDING, settled_at=None), DECLARATION, **common)
    # two records under the conversion key, even two equal ones, are not one conversion record
    record = transaction.metadata[0]
    assert record[0] == CONVERSION_METADATA_KEY and len(transaction.metadata) == 1
    for metadata in ((record, record), (record, (CONVERSION_METADATA_KEY, record[1].replace("3333333", "3333334")))):
        with pytest.raises(ConversionError):
            read_posted_conversion(replace(transaction, metadata=metadata), DECLARATION, **common)


def test_a_transaction_with_the_derived_id_that_is_not_a_conversion_blocks_closing(fx):
    # Someone posts a transaction under alice's derived id before the conversion.
    tx_id = conversion_transaction_id(DECLARATION, "acct-alice")
    forged = Transaction(
        transaction_id=tx_id,
        entries=(
            Entry(f"{tx_id}:0", tx_id, "acct-platform-usd", Money(-5, Currency.USD), 0),
            Entry(f"{tx_id}:1", tx_id, "acct-reserve-usd", Money(5, Currency.USD), 1),
        ),
        purpose_code="OTHER", initiator_id="mallory", initiated_at=FUNDED_AT, settled_at=FUNDED_AT,
        status=TransactionStatus.SETTLED, external_refs=(), compliance_decision=None, metadata=(),
    )
    fx.ledger.post(forged)
    with pytest.raises(ConversionReplayError):
        fx.convert("acct-alice", "acct-alice-usd")
    with pytest.raises(ConversionError):
        fx.convert_all()
    with pytest.raises(ConversionError):
        fx.close()
