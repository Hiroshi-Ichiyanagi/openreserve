"""Measured defects, each pinned by a test of the behaviour we want.

Context: these tests accompany the design for a hypothetical scenario
(docs/hypothetical/W_CHANGEOVER_DESIGN.md). "W" in that document is a
fictional scenario identifier; nothing here forecasts or offers a currency.
The defects below, however, are real properties of the code as it was
measured, and they matter whatever the scenario.

Each test asserts the behaviour we want. A defect that is still present is
marked ``xfail(strict=True)`` with the exact exception type it produces
(``raises=``). Consequences:

* If the defect is still present, the test is reported as XFAIL.
* If the test fails for any other reason (a different exception, a broken
  precondition), it is reported as a normal FAILURE, so a changed API cannot
  hide behind the marker.
* Once the defect is fixed, the test passes, ``strict=True`` turns that XPASS
  into a FAILURE, and whoever fixed it removes the marker (and, where the fix
  changes the API, adapts the test to the new API without weakening what it
  checks).

State: defect (a) is fixed for reserve artifacts (the Merkle-sum tree) and
defects (b) to (g) are fixed; the tests of (a), (c) and (e) were adapted to
the new interfaces. One strict expected failure stays on purpose: the legacy
hash-only tree, which still serves the intent and execution commitments,
lets two leaf lists with different totals share a root. It is fixed when
those commitments move to a count-bound tree.

Preconditions use ``pytest.fail`` (not ``assert``) so that a broken setup is
never mistaken for the defect.
"""

from __future__ import annotations

import copy
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import (
    Currency,
    Money,
    OwnerType,
    UnbalancedTransactionError,
)
from openreserve.transparency.merkle import (
    MerkleLeaf,
    MerkleTree,
    verify_proof_with_node_prefix,
)
from openreserve.transparency.proofs import ProofOfReservesGenerator
from openreserve.transparency.snapshot import (
    LiabilitiesSnapshot,
    LiabilitiesSnapshotGenerator,
    verify_snapshot_inclusion,
)
from openreserve.transparency.sumtree import (
    DOMAIN,
    SumTree,
    SumTreeContext,
    SumTreeError,
    verify_sum_tree_inclusion,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
SALT_KEY = bytes(range(32))  # a test key; a real one is an operator secret
INSTITUTION = "example-institution"


@pytest.fixture
def ledger():
    storage = SQLiteLedgerStorage(":memory:")
    yield Ledger(storage)
    storage.close()


def _move(ledger: Ledger, src, dst, amount: Money) -> None:
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, amount)
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


def _abc_leaves() -> list[MerkleLeaf]:
    return [
        MerkleLeaf(account_id="alice", balance_cents=300_000, currency_code="JPY"),
        MerkleLeaf(account_id="bob", balance_cents=120_000, currency_code="JPY"),
        MerkleLeaf(account_id="carol", balance_cents=55_000, currency_code="JPY"),
    ]


# ---------------------------------------------------------------------------
# (a) Totals are not bound to the Merkle root.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Kept on purpose: the legacy hash-only tree (transparency/merkle.py) is frozen "
        "so that published artifacts and the intent and execution commitments keep "
        "their roots. Its inner nodes hash only child hashes and an odd last node is "
        "paired with itself, so leaf lists [a,b,c] and [a,b,c,c] share one root while "
        "their totals differ (475000 vs 530000). Reserve artifacts use the Merkle-sum "
        "tree instead (next test). This marker goes away in stage B, when the intent "
        "and execution commitments move to a count-bound tree."
    ),
)
def test_a_legacy_tree_different_totals_do_not_share_a_root():
    abc = _abc_leaves()
    abcc = abc + [abc[-1]]
    tree_abc = MerkleTree(abc)
    tree_abcc = MerkleTree(abcc)

    if tree_abc.total_balance_cents() == tree_abcc.total_balance_cents():
        pytest.fail("precondition: the two leaf sets must have different totals")

    # Wanted: two leaf sets with different totals never share a root.
    assert tree_abc.root_hash != tree_abcc.root_hash, (
        "same root for different totals: "
        f"{tree_abc.total_balance_cents()} vs {tree_abcc.total_balance_cents()}"
    )


# Fixed for reserve artifacts: the Merkle-sum tree binds leaf count and total to the root.
def test_a_root_binds_total_no_duplicate_leaf_ambiguity():
    context = SumTreeContext(
        kind="liabilities",
        institution_id=INSTITUTION,
        currency="JPY",
        minor_units=0,
        as_of="2026-01-01T00:00:00.000000Z",
    )
    abc = [(leaf.account_id, leaf.balance_cents) for leaf in _abc_leaves()]

    # The legacy duplication case cannot be expressed: a second leaf for carol is refused.
    with pytest.raises(SumTreeError):
        SumTree.build(context, abc + [abc[-1]], salt_key=SALT_KEY)

    # The closest expressible case: another slot holding carol's amount.
    tree_abc = SumTree.build(context, abc, salt_key=SALT_KEY)
    tree_abcd = SumTree.build(context, abc + [("dave", abc[-1][1])], salt_key=SALT_KEY)
    if tree_abc.total == tree_abcd.total:
        pytest.fail("precondition: the two leaf sets must have different totals")
    assert (tree_abc.total, tree_abcd.total) == (475_000, 530_000)

    # Wanted: two leaf sets with different totals never share a root.
    assert tree_abc.root != tree_abcd.root, (
        f"same root for different totals: {tree_abc.total} vs {tree_abcd.total}"
    )

    # And the total next to a root cannot be changed while the root is kept: a holder's
    # check against such a summary fails.
    snapshot = LiabilitiesSnapshot.from_balances(
        institution_id=INSTITUTION,
        currency_code="JPY",
        minor_units=0,
        snapshot_at=T,
        balances=abc,
        salt_key=SALT_KEY,
    )
    summary = snapshot.to_public_summary()
    if summary["liabilities"] != tree_abc.head():
        pytest.fail("precondition: the snapshot's liabilities tree is the abc tree")
    proof = snapshot.proof_for("bob")
    assert verify_snapshot_inclusion(proof, summary, expected_slot_id="bob", expected_kind="liabilities").ok is True
    for understated in ("474999", "420000", "0"):
        forged = copy.deepcopy(summary)
        forged["liabilities"]["total"] = understated  # the root is kept
        result = verify_snapshot_inclusion(proof, forged, expected_slot_id="bob", expected_kind="liabilities")
        assert (result.ok, result.reason) == (False, "TOTAL_MISMATCH")
    inflated_count = copy.deepcopy(summary)
    inflated_count["liabilities"]["leaf_count"] = "4"
    result = verify_snapshot_inclusion(proof, inflated_count, expected_slot_id="bob", expected_kind="liabilities")
    assert (result.ok, result.reason) == (False, "COUNT_MISMATCH")


# ---------------------------------------------------------------------------
# (b) MerkleProof.verify() rejects a genuine proof.
# ---------------------------------------------------------------------------


# Fixed: MerkleProof.verify() and verify_proof_with_node_prefix() share one routine.
def test_b_merkleproof_verify_accepts_genuine_proof():
    tree = MerkleTree(_abc_leaves())
    proof = tree.proof_for("bob")

    if len(proof.steps) < 2:
        pytest.fail("precondition: the proof must have at least two steps")
    if not verify_proof_with_node_prefix(proof):
        pytest.fail("precondition: the reference verifier must accept this proof")

    # Wanted: the method on the proof object agrees with the reference verifier.
    assert proof.verify() is True


# ---------------------------------------------------------------------------
# (c) Ledger.fx_convert cannot be posted.
# ---------------------------------------------------------------------------


# Fixed: both legs go through one clearing account per currency (core/ledger.py, fx_convert).
def test_c_fx_convert_can_be_posted(ledger: Ledger):
    platform_jpy = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform-jpy")
    holder_jpy = ledger.open_account(OwnerType.USER, Currency.JPY, "holder-jpy")
    holder_usd = ledger.open_account(OwnerType.USER, Currency.USD, "holder-usd")
    clearing_jpy = ledger.open_account(OwnerType.CLEARING, Currency.JPY, "clearing-jpy")
    clearing_usd = ledger.open_account(OwnerType.CLEARING, Currency.USD, "clearing-usd")
    _move(ledger, platform_jpy, holder_jpy, Money.from_units(15_000, Currency.JPY))

    # The old call, with one account for both currencies as fifth positional argument, could
    # never be posted. It now fails where it is written.
    old_style = TransactionBuilder("FX_CONVERT", "test", initiated_at=T)
    with pytest.raises(TypeError):
        old_style.fx_convert(
            holder_jpy.account_id,
            Money.from_units(15_000, Currency.JPY),
            holder_usd.account_id,
            Money.from_units(100, Currency.USD),
            clearing_jpy.account_id,
        )

    b = TransactionBuilder("FX_CONVERT", "test", initiated_at=T)
    b.fx_convert(
        holder_jpy.account_id,
        Money.from_units(15_000, Currency.JPY),
        holder_usd.account_id,
        Money.from_units(100, Currency.USD),
        from_clearing_account_id=clearing_jpy.account_id,
        to_clearing_account_id=clearing_usd.account_id,
    )
    try:
        tx = b.build()
    except Exception as exc:  # the defect was at post(), not at build()
        pytest.fail(f"precondition: build() must succeed, got {type(exc).__name__}: {exc}")

    ledger.post(tx)  # before the fix: UnbalancedTransactionError
    ledger.settle(tx.transaction_id, settled_at=T)

    assert ledger.balance(holder_jpy.account_id, as_of=T).cents == 0
    assert ledger.balance(holder_usd.account_id, as_of=T).cents == 10_000
    # the two legs meet in the clearing accounts, one per currency
    assert ledger.balance(clearing_jpy.account_id, as_of=T).cents == 15_000
    assert ledger.balance(clearing_usd.account_id, as_of=T).cents == -10_000
    assert [(e.account_id, e.amount.cents, e.amount.currency) for e in tx.entries] == [
        (holder_jpy.account_id, -15_000, Currency.JPY),
        (clearing_jpy.account_id, 15_000, Currency.JPY),
        (clearing_usd.account_id, -10_000, Currency.USD),
        (holder_usd.account_id, 10_000, Currency.USD),
    ]

    # a clearing account of the wrong currency is still refused when posting
    wrong = TransactionBuilder("FX_CONVERT", "test", initiated_at=T)
    wrong.fx_convert(
        holder_jpy.account_id,
        Money.from_units(1, Currency.JPY),
        holder_usd.account_id,
        Money.from_units(1, Currency.USD),
        from_clearing_account_id=clearing_usd.account_id,
        to_clearing_account_id=clearing_jpy.account_id,
    )
    with pytest.raises(UnbalancedTransactionError):
        ledger.post(wrong.build())


# ---------------------------------------------------------------------------
# (d) The bundled SQLite store overflows at 10 units of an 18-decimal currency.
# ---------------------------------------------------------------------------


# Fixed: amounts are stored as canonical decimal text (core/storage.py, schema version 1).
def test_d_store_holds_ten_units_of_18_decimal_currency(ledger: Ledger):
    if Currency.JPYC.minor_units != 18:
        pytest.fail("precondition: JPYC must have 18 decimals")
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPYC, "platform")
    holder = ledger.open_account(OwnerType.USER, Currency.JPYC, "holder")

    # Boundary: 9 units (9 * 10**18 < 2**63 - 1) must already work today.
    try:
        _move(ledger, platform, holder, Money.from_units(9, Currency.JPYC))
    except Exception as exc:
        pytest.fail(f"precondition: 9 JPYC must post, got {type(exc).__name__}: {exc}")

    holder2 = ledger.open_account(OwnerType.USER, Currency.JPYC, "holder-2")
    _move(ledger, platform, holder2, Money.from_units(10, Currency.JPYC))  # today: OverflowError

    assert ledger.balance(holder2.account_id, as_of=T).cents == 10 * 10**18


# ---------------------------------------------------------------------------
# (e) Unsalted leaves: a leaf hash is a function of public inputs only.
# ---------------------------------------------------------------------------


def _sum_tree_leaf_hash(context_digest: bytes, slot_id: str, amount: int, salt: bytes) -> str:
    """The documented leaf hash of the Merkle-sum tree, written out here so that the
    attacker below uses nothing but the public specification."""
    lp = lambda b: len(b).to_bytes(4, "big") + b
    return hashlib.sha256(
        DOMAIN + b"\x00" + context_digest + lp(slot_id.encode()) + amount.to_bytes(16, "big") + salt + lp(b"")
    ).hexdigest()


# Fixed for reserve artifacts: leaves of the Merkle-sum tree carry a keyed salt.
def test_e_sibling_balance_cannot_be_guessed_from_public_inputs(ledger: Ledger):
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
    bob = ledger.open_account(OwnerType.USER, Currency.JPY, "bob")
    carol = ledger.open_account(OwnerType.USER, Currency.JPY, "carol")
    dave = ledger.open_account(OwnerType.USER, Currency.JPY, "dave")
    _move(ledger, platform, alice, Money.from_units(300_000, Currency.JPY))
    _move(ledger, platform, bob, Money.from_units(120_000, Currency.JPY))
    _move(ledger, platform, carol, Money.from_units(55_000, Currency.JPY))
    _move(ledger, platform, dave, Money.from_units(9_000, Currency.JPY))
    others = {bob.account_id: 120_000, carol.account_id: 55_000, dave.account_id: 9_000}

    generator = LiabilitiesSnapshotGenerator(ledger, institution_id=INSTITUTION, salt_key=SALT_KEY)
    snapshot = generator.generate(Currency.JPY, snapshot_at=T)
    summary = snapshot.to_public_summary()
    alice_proof = snapshot.proof_for(alice.account_id)  # what alice legitimately receives
    if len(alice_proof["path"]) != 2:
        pytest.fail("precondition: a four-leaf tree gives a two-step proof")
    if not verify_snapshot_inclusion(alice_proof, summary, expected_slot_id=alice.account_id, expected_kind="liabilities").ok:
        pytest.fail("precondition: alice's proof must verify")
    hashes_in_proof = {step["hash"] for step in alice_proof["path"]}
    tree_hashes = set(snapshot.liabilities.leaf_hashes())
    if not hashes_in_proof & tree_hashes:
        pytest.fail("precondition: alice's proof contains the leaf hash of one other account")

    # The attacker knows only public inputs: the other accounts' ids, the full context, the
    # leaf encoding and a range of plausible balances. The salts available to them are the
    # all-zero salt and alice's own salt. No field of another leaf is copied.
    context_digest = snapshot.liabilities.context.digest()
    available_salts = [bytes(32), bytes.fromhex(alice_proof["leaf"]["salt"])]
    recovered = None
    for account_id in others:
        for salt in available_salts:
            for guess in range(0, 150_001):  # every amount; each true balance is in the range
                candidate = _sum_tree_leaf_hash(context_digest, account_id, guess, salt)
                if candidate in hashes_in_proof or candidate in tree_hashes:
                    recovered = (account_id, guess)
    if not all(0 <= amount <= 150_000 for amount in others.values()):
        pytest.fail("precondition: the guesses must include every true balance")

    # Wanted: the leaf hash depends on something the attacker does not know.
    assert recovered is None, f"a balance was recovered from alice's proof: {recovered}"

    # With the right salt the same guess does reproduce the hash: the salt is what was missing.
    neighbour = snapshot.liabilities.leaves[snapshot.liabilities.index_of(bob.account_id)]
    assert _sum_tree_leaf_hash(context_digest, bob.account_id, 120_000, neighbour.salt) in tree_hashes

    # The same account with the same balance has different leaf hashes in two snapshots.
    later = generator.generate(Currency.JPY, snapshot_at=T + timedelta(days=1))
    if later.liabilities.total != snapshot.liabilities.total:
        pytest.fail("precondition: balances are unchanged one day later")
    assert tree_hashes.isdisjoint(later.liabilities.leaf_hashes())

    # What a proof does disclose is documented and pinned: the exact sum of each sibling
    # subtree (so usually one other account's amount), never an identifier.
    result = verify_sum_tree_inclusion(
        alice_proof, expected_root=summary["liabilities"]["root"], expected_slot_id=alice.account_id
    )
    disclosed = result.details["disclosed"]
    assert sorted(size for size, _ in disclosed) == [1, 2]
    assert sum(amount for _, amount in disclosed) == 184_000
    single_leaf_sum = next(amount for size, amount in disclosed if size == 1)
    assert single_leaf_sum in others.values()
    proof_text = str(alice_proof)
    for account_id in others:
        assert account_id not in proof_text


# ---------------------------------------------------------------------------
# (f) The as-of balance compares timestamp text, not instants.
# ---------------------------------------------------------------------------


# Fixed: the store compares instants in Python (core/storage.py), never text in SQL.
def test_f_as_of_balance_compares_instants_not_text(ledger: Ledger):
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    holder = ledger.open_account(OwnerType.USER, Currency.JPY, "holder")
    _move(ledger, platform, holder, Money.from_units(100, Currency.JPY))  # settled at T

    one_hour_before = T - timedelta(hours=1)
    one_hour_after = T + timedelta(hours=1)
    before_plus9 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=9)))
    after_minus5 = datetime(2025, 12, 31, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    if before_plus9 != one_hour_before or after_minus5 != one_hour_after:
        pytest.fail("precondition: the offset cut-offs must be the same instants as the UTC ones")
    if ledger.balance(holder.account_id, as_of=one_hour_before).cents != 0:
        pytest.fail("precondition: one hour before settlement, given in UTC, the balance is 0")
    if ledger.balance(holder.account_id, as_of=one_hour_after).cents != 100:
        pytest.fail("precondition: one hour after settlement, given in UTC, the balance is 100")

    # Wanted: the same instant gives the same balance, whatever offset it is written in.
    got_before = ledger.balance(holder.account_id, as_of=before_plus9).cents
    got_after = ledger.balance(holder.account_id, as_of=after_minus5).cents
    assert (got_before, got_after) == (0, 100), (
        f"balance one hour before settlement (+09:00): {got_before}, expected 0; "
        f"one hour after (-05:00): {got_after}, expected 100"
    )


# ---------------------------------------------------------------------------
# (g) A negative customer balance lowers the published liabilities.
# ---------------------------------------------------------------------------


# Fixed: negative balances are reported as receivables and never netted (transparency/proofs.py).
def test_g_negative_customer_balance_does_not_lower_liabilities(ledger: Ledger):
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve")
    a = ledger.open_account(OwnerType.USER, Currency.JPY, "a")
    b = ledger.open_account(OwnerType.USER, Currency.JPY, "b")
    _move(ledger, platform, reserve, Money.from_units(50, Currency.JPY))
    _move(ledger, platform, a, Money.from_units(100, Currency.JPY))
    _move(ledger, b, platform, Money.from_units(60, Currency.JPY))  # b is overdrawn

    if ledger.balance(a.account_id, as_of=T).cents != 100:
        pytest.fail("precondition: a holds 100")
    if ledger.balance(b.account_id, as_of=T).cents != -60:
        pytest.fail("precondition: b holds -60")
    if ledger.balance(reserve.account_id, as_of=T).cents != 50:
        pytest.fail("precondition: reserves are 50")

    proof, _tree = ProofOfReservesGenerator(ledger).generate(Currency.JPY, snapshot_at=T)

    # Wanted: what is owed to a is not reduced by what b owes, and 50 does not cover 100.
    assert (proof.user_liabilities_total_cents, proof.is_solvent) == (100, False), (
        f"published liabilities {proof.user_liabilities_total_cents} (expected 100), "
        f"is_solvent {proof.is_solvent} (expected False)"
    )
