"""The liabilities snapshot (design section 7).

Positive customer balances go to a liabilities tree, negative ones to a receivables tree,
zero balances to neither; the two are never netted. Expected values come from
tests/vectors/snapshot_expected_v1.json.
"""

from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # helpers shared with the other test modules

import pytest

import openreserve.core.ledger as ledger_mod
import openreserve.core.storage as storage_mod
import openreserve.transparency.snapshot as snapshot_mod
from openreserve.core.exact import NaiveDatetimeError, canonical_json, sha256_hex
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Account, Currency, Money, OwnerType
from openreserve.transparency.proofs import ProofOfReservesGenerator
from openreserve.transparency.snapshot import (
    FORMAT_SNAPSHOT,
    FORMAT_SNAPSHOT_EXPORT,
    LiabilitiesSnapshot,
    LiabilitiesSnapshotGenerator,
    audit_snapshot_export,
    collect_user_balances,
    verify_snapshot_inclusion,
)
from openreserve.transparency.sumtree import SumTree, SumTreeContext, SumTreeError

EXPECTED = json.loads((Path(__file__).parent / "vectors" / "snapshot_expected_v1.json").read_text(encoding="utf-8"))
KEY = bytes(range(32))
INSTITUTION = "example-institution"
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))
ABC_SUMMARY_HASH = "c981cf21c98a294c7ba4db280c0939d9ec984c82b3b0c82570d0fa1f8f6dd4ed"
OVERDRAFT_SUMMARY_HASH = "48756d5cc77a4134b2f5c18149b450ad323ed765048c4ee2558199a74b96263d"


class Fixture:
    def __init__(self) -> None:
        self.storage = SQLiteLedgerStorage(":memory:")
        self.ledger = Ledger(self.storage)

    def open(self, account_id: str, owner_type: OwnerType = OwnerType.USER, currency: Currency = Currency.JPY,
             created_at: datetime = T) -> str:
        self.storage.save_account(
            Account(account_id=account_id, owner_type=owner_type, currency=currency, label=account_id, created_at=created_at)
        )
        return account_id

    def move(self, src: str, dst: str, amount: int, *, at: datetime = T, settle: bool = True,
             currency: Currency = Currency.JPY) -> str:
        b = TransactionBuilder("MOVE", "test", initiated_at=at)
        b.transfer(src, dst, Money(cents=amount, currency=currency))
        self.ledger.post(b.build())
        if settle:
            self.ledger.settle(b.transaction_id, settled_at=at)
        return b.transaction_id

    def generator(self) -> LiabilitiesSnapshotGenerator:
        return LiabilitiesSnapshotGenerator(self.ledger, institution_id=INSTITUTION, salt_key=KEY)

    def snapshot(self, currency: Currency = Currency.JPY, at: datetime = T) -> LiabilitiesSnapshot:
        return self.generator().generate(currency, at)


def abc_fixture(order=("alice", "bob", "carol")) -> Fixture:
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    amounts = {"alice": 300_000, "bob": 120_000, "carol": 55_000}
    for account_id in order:
        f.open(account_id)
    for account_id in order:
        f.move("platform", account_id, amounts[account_id])
    return f


def overdraft_fixture() -> Fixture:
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    f.open("reserve", OwnerType.RESERVE)
    f.open("a")
    f.open("b")
    f.move("platform", "reserve", 50)
    f.move("platform", "a", 100)
    f.move("b", "platform", 60)  # b pays without funds: balance -60
    return f


# ---------------------------------------------------------------------------
# Pinned snapshots
# ---------------------------------------------------------------------------


def test_abc_snapshot_matches_the_pinned_values():
    snapshot = abc_fixture().snapshot()
    summary = snapshot.to_public_summary()
    assert summary == EXPECTED["abc_snapshot_summary"]["summary"]
    assert summary["liabilities"] == {
        "root": "15999a7b45465f066c26266c9c0371f430116f646c810dc30196623faba05b3a",
        "total": "475000",
        "leaf_count": "3",
    }
    assert summary["receivables"] == {
        "root": "f7b3370b9761b2dcdbb71edfb057c32c7522b2ec5ac81665720716e2b5afd79c",
        "total": "0",
        "leaf_count": "0",
    }
    assert canonical_json(summary).decode("utf-8") == EXPECTED["abc_snapshot_summary"]["canonical_utf8"]
    assert snapshot.summary_hash() == ABC_SUMMARY_HASH == EXPECTED["abc_snapshot_summary"]["summary_hash"]
    assert snapshot.liabilities.head() == EXPECTED["abc_liabilities"]
    assert snapshot.receivables.head() == EXPECTED["empty_receivables_same_context"]
    assert snapshot.snapshot_at == "2026-01-01T00:00:00.000000Z"
    assert summary["format"] == FORMAT_SNAPSHOT
    assert summary["inclusion_rule"] == "SETTLED_AT_OR_BEFORE_SNAPSHOT"
    assert summary["population_rule"] == "USER_ACCOUNTS_WITH_NONZERO_BALANCE"


def test_overdraft_snapshot_keeps_liabilities_and_receivables_apart():
    f = overdraft_fixture()
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()
    assert summary == EXPECTED["overdraft_snapshot_summary"]["summary"]
    assert summary["liabilities"] == {
        "root": "3c94a9c23690aa558bb928840782df5c7caeb430a5ca9ecfc6d91ae464b7b724", "total": "100", "leaf_count": "1",
    }
    assert summary["receivables"] == {
        "root": "bf7af395aa86433da9cca0bd0bfc6cb15e1c5f734284d387facb84478d04b50a", "total": "60", "leaf_count": "1",
    }
    assert snapshot.summary_hash() == OVERDRAFT_SUMMARY_HASH
    assert snapshot.liabilities.total == 100 and snapshot.receivables.total == 60
    assert [leaf.slot_id for leaf in snapshot.liabilities.leaves] == ["a"]
    assert [(leaf.slot_id, leaf.amount) for leaf in snapshot.receivables.leaves] == [("b", 60)]


def test_legacy_generator_no_longer_nets_a_negative_balance():
    f = overdraft_fixture()
    proof, tree = ProofOfReservesGenerator(f.ledger).generate(Currency.JPY, snapshot_at=T)
    after = EXPECTED["overdraft_snapshot_summary"]["legacy_after_fix"]
    before = EXPECTED["overdraft_snapshot_summary"]["legacy_before_fix"]
    assert (before["user_liabilities_total"], before["is_solvent"]) == ("40", True)  # what 0.1.x published
    assert proof.user_liabilities_total_cents == 100 == int(after["user_liabilities_total"])
    assert proof.user_receivables_count == 1 == int(after["user_receivables_count"])
    assert proof.user_receivables_total_cents == 60 == int(after["user_receivables_total"])
    assert proof.is_solvent is False and after["is_solvent"] is False
    assert proof.reserve_assets_total_cents == 50
    assert proof.user_liabilities_count == 1  # accounts that have a legacy leaf
    assert tree.leaf_count == 1 and tree.total_balance_cents() == 100
    with pytest.raises(ValueError):
        tree.proof_for("b")  # the debtor has no legacy leaf
    summary = proof.to_public_summary()
    assert summary["user_liabilities_total"] == 100
    assert summary["user_receivables_count"] == 1
    assert summary["user_receivables_total"] == 60
    assert summary["is_solvent"] is False


def test_legacy_generator_is_unchanged_without_negative_balances():
    f = abc_fixture()
    f.open("zero")  # a zero-balance account keeps its legacy leaf
    proof, tree = ProofOfReservesGenerator(f.ledger).generate(Currency.JPY, snapshot_at=T)
    assert proof.user_receivables_count == 0 and proof.user_receivables_total_cents == 0
    assert proof.user_liabilities_total_cents == 475_000
    assert proof.user_liabilities_count == 4
    assert tree.leaf_count == 4
    assert tree.proof_for("zero").leaf.balance_cents == 0
    summary = proof.to_public_summary()
    assert (summary["user_receivables_count"], summary["user_receivables_total"]) == (0, 0)
    assert summary["user_count"] == 4


def test_only_negative_balances_gives_the_legacy_placeholder_and_no_liabilities():
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    f.open("b")
    f.move("b", "platform", 60)
    proof, tree = ProofOfReservesGenerator(f.ledger).generate(Currency.JPY, snapshot_at=T)
    assert proof.user_liabilities_total_cents == 0 and proof.user_liabilities_count == 0
    assert (proof.user_receivables_count, proof.user_receivables_total_cents) == (1, 60)
    assert tree.leaf_count == 1 and tree.root_hash == "62d32face721021db81a4f51cd92801934df71977a317ba6f5781b62b5792e8b"
    snapshot = f.snapshot()
    assert (snapshot.liabilities.leaf_count, snapshot.receivables.leaf_count, snapshot.receivables.total) == (0, 1, 60)


# ---------------------------------------------------------------------------
# What a snapshot contains
# ---------------------------------------------------------------------------


def test_zero_balance_account_has_no_leaf():
    f = abc_fixture()
    f.open("zero")
    snapshot = f.snapshot()
    assert snapshot.summary_hash() == ABC_SUMMARY_HASH
    with pytest.raises(KeyError):
        snapshot.proof_for("zero")
    with pytest.raises(KeyError):
        snapshot.proof_for("platform")  # not a customer account
    with pytest.raises(KeyError):
        snapshot.proof_for("nobody")
    assert ("zero", 0) in collect_user_balances(f.ledger, Currency.JPY, T)


def test_account_brought_back_to_zero_has_no_leaf():
    f = abc_fixture()
    f.move("carol", "platform", 55_000)
    snapshot = f.snapshot()
    assert snapshot.liabilities.leaf_count == 2 and snapshot.liabilities.total == 420_000
    assert snapshot.liabilities.head() == EXPECTED["two_leaf_alice_bob"]
    with pytest.raises(KeyError):
        snapshot.proof_for("carol")


def test_pending_transfer_is_not_in_a_leaf():
    f = abc_fixture()
    f.move("platform", "alice", 7_000, at=T - timedelta(days=1), settle=False)  # pending, started earlier
    f.open("new")
    f.move("platform", "new", 9_000, settle=False)
    snapshot = f.snapshot()
    assert snapshot.summary_hash() == ABC_SUMMARY_HASH
    assert snapshot.proof_for("alice")["leaf"]["amount"] == "300000"
    with pytest.raises(KeyError):
        snapshot.proof_for("new")


def test_snapshot_of_the_past_does_not_change_when_things_happen_later():
    f = abc_fixture()
    before = f.snapshot()
    later = T + timedelta(days=30)
    f.open("late", created_at=later)
    f.move("platform", "late", 42_000, at=later)
    f.move("platform", "alice", 1, at=later)
    f.move("bob", "carol", 20_000, at=later)
    again = f.snapshot()
    assert again.summary_hash() == before.summary_hash() == ABC_SUMMARY_HASH
    assert again.auditor_export() == before.auditor_export()
    now = f.snapshot(at=later)
    assert now.liabilities.total == 475_000 + 42_000 + 1 and now.liabilities.leaf_count == 4
    assert now.summary_hash() != ABC_SUMMARY_HASH
    with pytest.raises(KeyError):
        again.proof_for("late")


def test_settlement_at_the_instant_is_in_and_one_microsecond_later_is_out():
    f = abc_fixture()
    f.open("edge")
    f.move("platform", "edge", 5, at=T + timedelta(microseconds=1))
    assert f.snapshot().summary_hash() == ABC_SUMMARY_HASH
    assert f.snapshot(at=T + timedelta(microseconds=1)).liabilities.total == 475_005
    assert f.snapshot(at=T - timedelta(microseconds=1)).liabilities.leaf_count == 0


def test_same_instant_in_another_offset_gives_the_same_summary():
    f = abc_fixture()
    assert f.snapshot(at=datetime(2026, 1, 1, 9, 0, tzinfo=JST)).summary_hash() == ABC_SUMMARY_HASH
    assert f.snapshot(at=datetime(2025, 12, 31, 19, 0, tzinfo=timezone(timedelta(hours=-5)))).summary_hash() == ABC_SUMMARY_HASH


def test_root_does_not_depend_on_the_order_in_which_accounts_were_created():
    hashes = {abc_fixture(order).snapshot().summary_hash() for order in (
        ("alice", "bob", "carol"), ("carol", "bob", "alice"), ("bob", "carol", "alice"),
    )}
    assert hashes == {ABC_SUMMARY_HASH}


def test_other_currencies_and_account_types_are_not_in_the_snapshot():
    f = abc_fixture()
    f.open("platform-usd", OwnerType.PLATFORM, Currency.USD)
    f.open("alice-usd", OwnerType.USER, Currency.USD)
    f.move("platform-usd", "alice-usd", 12_345, currency=Currency.USD)
    f.open("reserve", OwnerType.RESERVE)
    f.open("fee", OwnerType.FEE)
    f.move("platform", "reserve", 1_000_000)
    f.move("platform", "fee", 3)
    assert f.snapshot().summary_hash() == ABC_SUMMARY_HASH
    usd = f.snapshot(Currency.USD)
    summary = usd.to_public_summary()
    assert (summary["currency"], summary["minor_units"]) == ("USD", "2")
    assert summary["liabilities"]["total"] == "12345" and summary["liabilities"]["leaf_count"] == "1"
    assert usd.summary_hash() != ABC_SUMMARY_HASH


def test_collect_user_balances_lists_every_customer_account_with_its_signed_balance():
    f = overdraft_fixture()
    f.open("zero")
    assert collect_user_balances(f.ledger, Currency.JPY, T) == [("a", 100), ("b", -60), ("zero", 0)]
    assert collect_user_balances(f.ledger, Currency.USD, T) == []
    assert collect_user_balances(f.ledger, Currency.JPY, T - timedelta(seconds=1)) == [("a", 0), ("b", 0), ("zero", 0)]
    with pytest.raises(NaiveDatetimeError):
        collect_user_balances(f.ledger, Currency.JPY, datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# Holder checks
# ---------------------------------------------------------------------------


def test_every_holder_proof_verifies_against_the_summary():
    f = overdraft_fixture()
    f.open("alice")
    f.open("bob")
    f.move("platform", "alice", 300_000)
    f.move("platform", "bob", 120_000)
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()
    expected = {"a": ("liabilities", 100), "alice": ("liabilities", 300_000), "bob": ("liabilities", 120_000),
                "b": ("receivables", 60)}
    for account_id, (kind, amount) in expected.items():
        proof = snapshot.proof_for(account_id)
        assert proof["context"]["kind"] == kind
        result = verify_snapshot_inclusion(proof, summary, expected_slot_id=account_id, expected_kind=kind)
        assert result.ok is True and result.reason is None
        assert result.details["kind"] == kind
        assert result.details["amount"] == amount
        assert result.details["slot_id"] == account_id
        assert result.details["total"] == int(summary[kind]["total"])
        assert result.details["leaf_count"] == int(summary[kind]["leaf_count"])
        assert result.details["summary_hash"] == snapshot.summary_hash()
    # a proof for one account is not a proof for another
    result = verify_snapshot_inclusion(snapshot.proof_for("alice"), summary, expected_slot_id="bob", expected_kind="liabilities")
    assert result.reason == "SLOT_MISMATCH"


def test_summary_with_a_changed_total_or_count_and_the_unchanged_root():
    snapshot = abc_fixture().snapshot()
    summary = snapshot.to_public_summary()
    proof = snapshot.proof_for("alice")
    for total in ("474999", "475001", "0", "300000"):
        forged = copy.deepcopy(summary)
        forged["liabilities"]["total"] = total
        assert verify_snapshot_inclusion(proof, forged, expected_slot_id="alice", expected_kind="liabilities").reason == "TOTAL_MISMATCH"
    for count in ("2", "4", "0"):
        forged = copy.deepcopy(summary)
        forged["liabilities"]["leaf_count"] = count
        assert verify_snapshot_inclusion(proof, forged, expected_slot_id="alice", expected_kind="liabilities").reason == "COUNT_MISMATCH"
    forged = copy.deepcopy(summary)
    forged["liabilities"]["root"] = "ab" * 32
    assert verify_snapshot_inclusion(proof, forged, expected_slot_id="alice", expected_kind="liabilities").reason == "ROOT_MISMATCH"
    # changing the other tree's head does not affect this holder's check
    forged = copy.deepcopy(summary)
    forged["receivables"]["total"] = "5"
    assert verify_snapshot_inclusion(proof, forged, expected_slot_id="alice", expected_kind="liabilities").ok


def test_proof_from_another_currency_instant_institution_or_kind_is_a_context_mismatch():
    f = abc_fixture()
    f.open("platform-usd", OwnerType.PLATFORM, Currency.USD)
    f.open("alice-usd", OwnerType.USER, Currency.USD)
    f.move("platform-usd", "alice-usd", 300_000, currency=Currency.USD)
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()

    other_currency = f.snapshot(Currency.USD).proof_for("alice-usd")
    assert verify_snapshot_inclusion(other_currency, summary, expected_slot_id="alice-usd", expected_kind="liabilities").reason == "CONTEXT_MISMATCH"
    other_instant = f.snapshot(at=T + timedelta(seconds=1)).proof_for("alice")
    assert verify_snapshot_inclusion(other_instant, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "CONTEXT_MISMATCH"
    other_institution = LiabilitiesSnapshotGenerator(f.ledger, institution_id="other", salt_key=KEY).generate(Currency.JPY, T)
    assert verify_snapshot_inclusion(other_institution.proof_for("alice"), summary, expected_slot_id="alice", expected_kind="liabilities").reason == "CONTEXT_MISMATCH"

    genuine = snapshot.proof_for("alice")
    for field, value in (("kind", "conversion-liabilities"), ("kind", "assets"), ("kind", None),
                         ("binding", "11" * 32), ("minor_units", "2"), ("currency", "USD"),
                         ("as_of", "2026-01-01T00:00:00.000001Z"), ("institution_id", "x")):
        changed = copy.deepcopy(genuine)
        changed["context"][field] = value
        assert verify_snapshot_inclusion(changed, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "CONTEXT_MISMATCH", field
    extra = copy.deepcopy(genuine)
    extra["context"]["note"] = "x"
    assert verify_snapshot_inclusion(extra, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "CONTEXT_MISMATCH"


def test_receivables_proof_is_checked_against_the_receivables_head():
    f = overdraft_fixture()
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()
    debtor = snapshot.proof_for("b")
    assert debtor["context"]["kind"] == "receivables"
    assert verify_snapshot_inclusion(debtor, summary, expected_slot_id="b", expected_kind="receivables").details["amount"] == 60
    forged = copy.deepcopy(summary)
    forged["receivables"]["total"] = "59"
    assert verify_snapshot_inclusion(debtor, forged, expected_slot_id="b", expected_kind="receivables").reason == "TOTAL_MISMATCH"
    # presenting the debt as a claim: the liabilities root does not contain it
    as_claim = copy.deepcopy(debtor)
    as_claim["context"]["kind"] = "liabilities"
    assert verify_snapshot_inclusion(as_claim, summary, expected_slot_id="b", expected_kind="liabilities").reason == "ROOT_MISMATCH"
    # swapping the two heads in the summary does not help either
    swapped = copy.deepcopy(summary)
    swapped["liabilities"], swapped["receivables"] = summary["receivables"], summary["liabilities"]
    assert verify_snapshot_inclusion(debtor, swapped, expected_slot_id="b", expected_kind="receivables").reason == "ROOT_MISMATCH"


def test_a_creditor_listed_as_a_debtor_is_told_so():
    # The institution owes alice 300000 but books her into the receivables tree with that
    # magnitude: the published liabilities total drops by her amount, and her proof is a
    # genuine proof of the published receivables root with her exact amount.
    honest = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=T,
        balances=[("alice", 300_000), ("bob", 120_000), ("carol", -60)], salt_key=KEY,
    )
    cheat = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=T,
        balances=[("alice", -300_000), ("bob", 120_000), ("carol", -60)], salt_key=KEY,
    )
    assert honest.to_public_summary()["liabilities"]["total"] == "420000"
    summary = cheat.to_public_summary()
    assert summary["liabilities"]["total"] == "120000"
    proof = cheat.proof_for("alice")

    result = verify_snapshot_inclusion(proof, summary, expected_slot_id="alice", expected_kind="liabilities")
    assert (result.ok, result.reason) == (False, "KIND_MISMATCH")
    assert result.details["kind"] == "receivables"
    # the proof itself is genuine: it shows a debt of exactly her amount
    as_debt = verify_snapshot_inclusion(proof, summary, expected_slot_id="alice", expected_kind="receivables")
    assert as_debt.ok is True and as_debt.details["amount"] == 300_000 and as_debt.details["kind"] == "receivables"
    # the other way round: a debtor shown as a creditor is told so as well
    flipped = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=T,
        balances=[("alice", 300_000), ("bob", 120_000), ("carol", 60)], salt_key=KEY,
    )
    result = verify_snapshot_inclusion(
        flipped.proof_for("carol"), flipped.to_public_summary(), expected_slot_id="carol", expected_kind="receivables"
    )
    assert (result.ok, result.reason, result.details["kind"]) == (False, "KIND_MISMATCH", "liabilities")
    # each holder of the honest snapshot passes with the kind they expect
    for slot, kind in (("alice", "liabilities"), ("bob", "liabilities"), ("carol", "receivables")):
        assert verify_snapshot_inclusion(
            honest.proof_for(slot), honest.to_public_summary(), expected_slot_id=slot, expected_kind=kind
        ).ok


@pytest.mark.parametrize("kind", ["assets", "Liabilities", "conversion-liabilities", "", None, 0, b"liabilities"])
def test_expected_kind_must_be_one_of_the_two_kinds(kind):
    snapshot = abc_fixture().snapshot()
    with pytest.raises(ValueError):
        verify_snapshot_inclusion(
            snapshot.proof_for("alice"), snapshot.to_public_summary(), expected_slot_id="alice", expected_kind=kind
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.pop("format"),
        lambda s: s.__setitem__("format", "openreserve.liabilities-snapshot/2"),
        lambda s: s.__setitem__("extra", "x"),
        lambda s: s.pop("receivables"),
        lambda s: s.__setitem__("inclusion_rule", "ALL"),
        lambda s: s.__setitem__("population_rule", "ALL_ACCOUNTS"),
        lambda s: s.__setitem__("minor_units", 0),
        lambda s: s.__setitem__("minor_units", "31"),
        lambda s: s.__setitem__("minor_units", "00"),
        lambda s: s.__setitem__("currency", "jpy"),
        lambda s: s.__setitem__("institution_id", ""),
        lambda s: s.__setitem__("snapshot_at", "2026-01-01T00:00:00+00:00"),
        lambda s: s["liabilities"].__setitem__("total", 475000),
        lambda s: s["liabilities"].__setitem__("total", "-1"),
        lambda s: s["liabilities"].__setitem__("total", "0475000"),
        lambda s: s["liabilities"].__setitem__("leaf_count", str(2**64)),
        lambda s: s["liabilities"].__setitem__("root", "AB" * 32),
        lambda s: s["liabilities"].pop("root"),
        lambda s: s["liabilities"].__setitem__("extra", "x"),
        lambda s: s.__setitem__("receivables", None),
        lambda s: s["receivables"].__setitem__("total", 1.5),
    ],
)
def test_malformed_summary(change):
    snapshot = abc_fixture().snapshot()
    proof = snapshot.proof_for("alice")
    summary = snapshot.to_public_summary()
    change(summary)
    assert verify_snapshot_inclusion(proof, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "MALFORMED_SUMMARY"
    export = snapshot.auditor_export()
    change(export["summary"])
    # a JSON number anywhere puts the whole export outside the canonical value profile
    has_number = any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in _walk(export["summary"]))
    expected = "MALFORMED_EXPORT" if has_number else "MALFORMED_SUMMARY"
    assert audit_snapshot_export(export, expected_summary_hash=ABC_SUMMARY_HASH).reason == expected


@pytest.mark.parametrize("summary", [None, 5, "summary", [], {}])
def test_summary_that_is_not_a_summary(summary):
    proof = abc_fixture().snapshot().proof_for("alice")
    assert verify_snapshot_inclusion(proof, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "MALFORMED_SUMMARY"


def test_proof_that_is_not_a_proof_gets_the_reason_of_the_tree_check():
    snapshot = abc_fixture().snapshot()
    summary = snapshot.to_public_summary()
    for document in (None, 5, [], {}, {"format": "x"}, snapshot.auditor_export(), summary):
        assert verify_snapshot_inclusion(document, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "UNSUPPORTED_FORMAT"
    proof = snapshot.proof_for("alice")
    no_context = copy.deepcopy(proof)
    no_context.pop("context")
    assert verify_snapshot_inclusion(no_context, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "MALFORMED_PROOF"
    bad_context = copy.deepcopy(proof)
    bad_context["context"] = "liabilities"
    assert verify_snapshot_inclusion(bad_context, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "MALFORMED_PROOF"
    with_payload = copy.deepcopy(proof)
    with_payload["leaf"]["payload"] = "x"
    assert verify_snapshot_inclusion(with_payload, summary, expected_slot_id="alice", expected_kind="liabilities").reason == "MALFORMED_PROOF"
    with pytest.raises(ValueError):
        verify_snapshot_inclusion(proof, summary, expected_slot_id="", expected_kind="liabilities")


# ---------------------------------------------------------------------------
# What is published
# ---------------------------------------------------------------------------


def _walk(node):
    yield node
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_public_summary_has_no_salt_no_account_id_and_no_number():
    f = overdraft_fixture()
    f.open("alice")
    f.move("platform", "alice", 300_000)
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()
    secrets = set()
    for tree in (snapshot.liabilities, snapshot.receivables):
        for leaf in tree.leaves:
            secrets.add(leaf.slot_id)
            secrets.add(leaf.salt.hex())
    assert {"a", "b", "alice"} <= secrets
    values = list(_walk(summary))
    for value in values:
        assert not isinstance(value, (int, float)) or isinstance(value, bool), value
        if isinstance(value, str):
            assert value not in secrets
    text = canonical_json(summary).decode("utf-8")
    for secret in secrets:
        if len(secret) > 1:  # one-letter ids occur inside ordinary words
            assert secret not in text
    assert "salt" not in text and "slot_id" not in text and "payload" not in text
    # every amount survives a parser that turns numbers into floats, because it is a string
    reparsed = json.loads(text, parse_int=float, parse_float=float)
    assert reparsed == summary
    assert sha256_hex(canonical_json(reparsed)) == snapshot.summary_hash()


# ---------------------------------------------------------------------------
# Auditor export
# ---------------------------------------------------------------------------


def test_auditor_export_passes_and_a_changed_amount_is_found():
    f = overdraft_fixture()
    snapshot = f.snapshot()
    export = snapshot.auditor_export()
    assert set(export) == {"format", "summary", "liabilities", "receivables"}
    assert export["format"] == FORMAT_SNAPSHOT_EXPORT
    result = audit_snapshot_export(export, expected_summary_hash=OVERDRAFT_SUMMARY_HASH)
    assert result.ok is True
    assert result.details == {
        "summary_hash": OVERDRAFT_SUMMARY_HASH,
        "liabilities_total": 100, "liabilities_leaf_count": 1,
        "receivables_total": 60, "receivables_leaf_count": 1,
    }

    def reason(fn, expected_hash=OVERDRAFT_SUMMARY_HASH):
        changed = copy.deepcopy(export)
        fn(changed)
        return audit_snapshot_export(changed, expected_summary_hash=expected_hash).reason

    assert reason(lambda e: e["liabilities"]["leaves"][0].__setitem__("amount", "101")) == "LIABILITIES_ROOT_MISMATCH"
    assert reason(lambda e: e["receivables"]["leaves"][0].__setitem__("amount", "59")) == "RECEIVABLES_ROOT_MISMATCH"
    assert reason(lambda e: e["receivables"]["leaves"].clear()) == "RECEIVABLES_ROOT_MISMATCH"
    assert reason(lambda e: e["liabilities"]["leaves"][0].__setitem__("amount", "-1")) == "LIABILITIES_MALFORMED_EXPORT"
    assert reason(lambda e: e["liabilities"].__setitem__("format", "x")) == "LIABILITIES_UNSUPPORTED_FORMAT"
    assert reason(lambda e: e.__setitem__("receivables", None)) == "RECEIVABLES_UNSUPPORTED_FORMAT"
    assert reason(lambda e: e["liabilities"]["context"].__setitem__("currency", "USD")) == "LIABILITIES_CONTEXT_MISMATCH"
    assert reason(lambda e: e["receivables"]["context"].__setitem__("kind", "liabilities")) == "RECEIVABLES_CONTEXT_MISMATCH"
    assert reason(lambda e: e["summary"]["liabilities"].__setitem__("total", "99")) == "SUMMARY_HASH_MISMATCH"
    assert reason(lambda e: None, expected_hash=ABC_SUMMARY_HASH) == "SUMMARY_HASH_MISMATCH"
    assert reason(lambda e: e.__setitem__("extra", "x")) == "MALFORMED_EXPORT"
    assert reason(lambda e: e.pop("liabilities")) == "MALFORMED_EXPORT"
    assert reason(lambda e: e.__setitem__("format", FORMAT_SNAPSHOT)) == "UNSUPPORTED_FORMAT"
    for document in (None, [], {}, snapshot.to_public_summary()):
        assert audit_snapshot_export(document, expected_summary_hash=OVERDRAFT_SUMMARY_HASH).reason == "UNSUPPORTED_FORMAT"
    with pytest.raises(ValueError):
        audit_snapshot_export(export, expected_summary_hash="xyz")


def test_auditor_finds_a_total_or_count_that_differs_from_the_root_and_a_slot_in_both_trees():
    liabilities_context = SumTreeContext("liabilities", INSTITUTION, "JPY", 0, "2026-01-01T00:00:00.000000Z")
    receivables_context = SumTreeContext("receivables", INSTITUTION, "JPY", 0, "2026-01-01T00:00:00.000000Z")
    liabilities = SumTree.build(liabilities_context, [("a", 100), ("x", 5)], salt_key=KEY)
    receivables = SumTree.build(receivables_context, [("b", 60), ("x", 5)], salt_key=KEY)
    with pytest.raises(SumTreeError):
        LiabilitiesSnapshot(liabilities=liabilities, receivables=receivables)  # the library refuses to build it

    summary = LiabilitiesSnapshot(
        liabilities=SumTree.build(liabilities_context, [("a", 100)], salt_key=KEY),
        receivables=SumTree.build(receivables_context, [("b", 60)], salt_key=KEY),
    ).to_public_summary()
    summary["liabilities"] = liabilities.head()
    summary["receivables"] = receivables.head()
    export = {"format": FORMAT_SNAPSHOT_EXPORT, "summary": summary,
              "liabilities": liabilities.export(), "receivables": receivables.export()}
    digest = sha256_hex(canonical_json(summary))
    assert audit_snapshot_export(export, expected_summary_hash=digest).reason == "SLOT_IN_BOTH_TREES"

    # a summary whose stated total differs from the one bound in its root
    honest = LiabilitiesSnapshot(
        liabilities=SumTree.build(liabilities_context, [("a", 100)], salt_key=KEY),
        receivables=SumTree.build(receivables_context, [("b", 60)], salt_key=KEY),
    )
    export = honest.auditor_export()
    export["summary"]["liabilities"]["total"] = "40"
    digest = sha256_hex(canonical_json(export["summary"]))
    assert audit_snapshot_export(export, expected_summary_hash=digest).reason == "LIABILITIES_HEAD_MISMATCH"
    export = honest.auditor_export()
    export["summary"]["receivables"]["leaf_count"] = "2"
    digest = sha256_hex(canonical_json(export["summary"]))
    assert audit_snapshot_export(export, expected_summary_hash=digest).reason == "RECEIVABLES_HEAD_MISMATCH"


def test_snapshot_refuses_inconsistent_trees():
    liabilities_context = SumTreeContext("liabilities", INSTITUTION, "JPY", 0, "2026-01-01T00:00:00.000000Z")
    good = SumTree.build(liabilities_context, [("a", 1)], salt_key=KEY)
    for other in (
        SumTreeContext("liabilities", INSTITUTION, "JPY", 0, "2026-01-01T00:00:00.000000Z"),
        SumTreeContext("receivables", INSTITUTION, "USD", 0, "2026-01-01T00:00:00.000000Z"),
        SumTreeContext("receivables", INSTITUTION, "JPY", 2, "2026-01-01T00:00:00.000000Z"),
        SumTreeContext("receivables", INSTITUTION, "JPY", 0, "2026-01-02T00:00:00.000000Z"),
        SumTreeContext("receivables", "other", "JPY", 0, "2026-01-01T00:00:00.000000Z"),
        SumTreeContext("receivables", INSTITUTION, "JPY", 0, "2026-01-01T00:00:00.000000Z", bytes(32)),
    ):
        with pytest.raises(SumTreeError):
            LiabilitiesSnapshot(liabilities=good, receivables=SumTree.build(other, [], salt_key=KEY))


def test_from_balances_needs_no_ledger_and_refuses_bad_input():
    snapshot = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=T,
        balances=[("alice", 300_000), ("bob", 120_000), ("carol", 55_000), ("zero", 0)], salt_key=KEY,
    )
    assert snapshot.summary_hash() == ABC_SUMMARY_HASH
    fictional = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION, currency_code="FICTIONAL_W", minor_units=2, snapshot_at=T,
        balances=[("a", 5), ("b", -7)], salt_key=KEY,
    )
    assert fictional.to_public_summary()["currency"] == "FICTIONAL_W"
    assert (fictional.liabilities.total, fictional.receivables.total) == (5, 7)
    for balances in ([("a", 1), ("a", -1)], [("a", 1), ("a", 0)], [("a", 1.0)], [("a", True)], [("a", "1")],
                     [("a", 10**38)], [("a", -(10**38))], [(5, 1)], [("a",)], ["a"]):
        with pytest.raises(SumTreeError):
            LiabilitiesSnapshot.from_balances(
                institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=T,
                balances=balances, salt_key=KEY,
            )
    with pytest.raises(NaiveDatetimeError):
        LiabilitiesSnapshot.from_balances(
            institution_id=INSTITUTION, currency_code="JPY", minor_units=0, snapshot_at=datetime(2026, 1, 1),
            balances=[], salt_key=KEY,
        )


# ---------------------------------------------------------------------------
# Inputs and determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [b"", bytes(31), "k" * 32, None, bytearray(32)])
def test_generator_refuses_a_bad_key(key):
    f = abc_fixture()
    with pytest.raises(ValueError):
        LiabilitiesSnapshotGenerator(f.ledger, institution_id=INSTITUTION, salt_key=key)


@pytest.mark.parametrize("institution_id", ["", None, 5, "x" * 256, "a\nb"])
def test_generator_refuses_a_bad_institution_id(institution_id):
    f = abc_fixture()
    with pytest.raises(ValueError):
        LiabilitiesSnapshotGenerator(f.ledger, institution_id=institution_id, salt_key=KEY)


def test_generator_refuses_a_naive_instant_and_needs_an_explicit_one():
    f = abc_fixture()
    with pytest.raises(NaiveDatetimeError):
        f.generator().generate(Currency.JPY, datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        f.generator().generate(Currency.JPY)  # type: ignore[call-arg]


def test_another_key_or_institution_gives_other_roots_and_the_same_totals():
    f = abc_fixture()
    base = f.snapshot()
    other_key = LiabilitiesSnapshotGenerator(f.ledger, institution_id=INSTITUTION, salt_key=bytes(range(1, 33))).generate(Currency.JPY, T)
    other_institution = LiabilitiesSnapshotGenerator(f.ledger, institution_id="other-institution", salt_key=KEY).generate(Currency.JPY, T)
    assert other_key.liabilities.root == "8e284b9c62c12f170e2b1a08718a621342615e796e25ec074987acb2b3ff1486"
    assert other_institution.liabilities.root == "72efb46a33630375c28c42722d0da7e49c8d1787a92266175188534295ee5757"
    assert len({base.liabilities.root, other_key.liabilities.root, other_institution.liabilities.root}) == 3
    assert base.liabilities.total == other_key.liabilities.total == other_institution.liabilities.total == 475_000


class _NowForbidden:
    """A stand-in for ``datetime`` whose clock raises."""

    @staticmethod
    def now(*args, **kwargs):
        raise AssertionError("datetime.now() called during snapshot generation (wall-clock leak)")

    fromisoformat = staticmethod(datetime.fromisoformat)


def test_generation_reads_no_clock():
    f = abc_fixture()
    assert not hasattr(snapshot_mod, "datetime")  # the module does not even import it at run time
    original = (ledger_mod.datetime, storage_mod.datetime)
    ledger_mod.datetime = _NowForbidden  # type: ignore[assignment]
    storage_mod.datetime = _NowForbidden  # type: ignore[assignment]
    try:
        snapshot = f.snapshot()
        export = snapshot.auditor_export()
        proof = snapshot.proof_for("alice")
        assert snapshot.summary_hash() == ABC_SUMMARY_HASH
        assert verify_snapshot_inclusion(proof, snapshot.to_public_summary(), expected_slot_id="alice", expected_kind="liabilities").ok
        assert audit_snapshot_export(export, expected_summary_hash=ABC_SUMMARY_HASH).ok
    finally:
        ledger_mod.datetime, storage_mod.datetime = original


def test_generation_is_reproducible():
    f = abc_fixture()
    a, b = f.snapshot(), f.snapshot()
    assert a.auditor_export() == b.auditor_export()
    assert a.proof_for("bob") == b.proof_for("bob")
    assert abc_fixture().snapshot().auditor_export() == a.auditor_export()


# ---------------------------------------------------------------------------
# Seeded mutations
# ---------------------------------------------------------------------------


def test_seeded_mutations_of_proof_summary_and_export_never_raise_and_are_never_accepted():
    from test_sumtree import _same, mutate  # the mutation helper of the tree tests

    f = overdraft_fixture()
    for name, amount in (("alice", 300_000), ("bob", 120_000), ("carol", 55_000)):
        f.open(name)
        f.move("platform", name, amount)
    snapshot = f.snapshot()
    summary = snapshot.to_public_summary()
    summary_hash = snapshot.summary_hash()
    export = snapshot.auditor_export()
    holders = ["a", "b", "alice", "bob", "carol"]
    rng = random.Random(77)

    done = accepted = 0
    while done < 3_000:
        slot = rng.choice(holders)
        proof = snapshot.proof_for(slot)
        candidate = mutate(proof, rng)
        if _same(candidate, proof):
            continue
        done += 1
        kind = proof["context"]["kind"]  # the holder knows whether they are owed or owe
        inner = snapshot_mod._verify_snapshot_inclusion(candidate, summary, slot, kind)
        outer = verify_snapshot_inclusion(candidate, summary, expected_slot_id=slot, expected_kind=kind)
        assert (inner.ok, inner.reason) == (outer.ok, outer.reason)
        accepted += outer.ok
    assert accepted == 0

    # A changed summary: either the holder's check fails, or the change is in the part this
    # holder's check does not read (the other tree's head) and then the summary hash differs.
    done = 0
    while done < 2_000:
        slot = rng.choice(holders)
        proof = snapshot.proof_for(slot)
        candidate = mutate(summary, rng)
        if _same(candidate, summary):
            continue
        done += 1
        kind = proof["context"]["kind"]
        outer = verify_snapshot_inclusion(proof, candidate, expected_slot_id=slot, expected_kind=kind)
        snapshot_mod._verify_snapshot_inclusion(proof, candidate, slot, kind)
        if outer.ok:
            assert outer.details["summary_hash"] != summary_hash
            other = "receivables" if proof["context"]["kind"] == "liabilities" else "liabilities"
            assert candidate[other] != summary[other]
            assert {k: v for k, v in candidate.items() if k != other} == {k: v for k, v in summary.items() if k != other}

    done = accepted = 0
    while done < 1_500:
        candidate = mutate(export, rng)
        if _same(candidate, export):
            continue
        done += 1
        inner = snapshot_mod._audit_snapshot_export(candidate, summary_hash)
        outer = audit_snapshot_export(candidate, expected_summary_hash=summary_hash)
        assert (inner.ok, inner.reason) == (outer.ok, outer.reason)
        accepted += outer.ok
    assert accepted == 0
