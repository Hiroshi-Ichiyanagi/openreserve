"""The legacy hash-only tree is frozen, and it has one verifier (design section 5).

The vectors in tests/vectors/legacy_merkle_v0.json were computed without the library. The
library must reproduce every root and accept every proof through both entry points,
``MerkleProof.verify()`` and ``verify_proof_with_node_prefix()``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.transparency.merkle as merkle_mod
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Account, Currency, Money, OwnerType
from openreserve.transparency.merkle import (
    MerkleLeaf,
    MerkleProof,
    MerkleProofStep,
    MerkleTree,
    verify_proof_with_node_prefix,
)
from openreserve.transparency.proofs import ProofOfReservesGenerator

VECTORS = json.loads((Path(__file__).parent / "vectors" / "legacy_merkle_v0.json").read_text(encoding="utf-8"))
CASES = VECTORS["cases"]
ABC_ROOT = "746446bf3f90b11cf1311d7b2e3193c81c3cb0d4dce4ef8416c85ae62cdf215c"
T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _leaves(case) -> list[MerkleLeaf]:
    return [
        MerkleLeaf(account_id=l["account_id"], balance_cents=int(l["balance_cents"]), currency_code=l["currency_code"])
        for l in case["leaves"]
    ]


def _abc_tree() -> MerkleTree:
    return MerkleTree(
        [
            MerkleLeaf("alice", 300_000, "JPY"),
            MerkleLeaf("bob", 120_000, "JPY"),
            MerkleLeaf("carol", 55_000, "JPY"),
        ]
    )


def _numbered_tree(n: int) -> MerkleTree:
    return MerkleTree([MerkleLeaf(f"acct-{i}", 1000 + i, "JPY") for i in range(n)])


# ---------- frozen bytes ----------


def test_the_vector_file_has_the_six_pinned_roots():
    roots = {c["name"].split(" ")[0]: c["root"] for c in CASES}
    assert len(CASES) == 6
    assert roots["one"] == "0654bedb45fcc90a73882fbaef3832269051d794ba4780fcc7cc192f2c65dd4b"
    assert roots["two"] == "de6dcbd79f37e74e0b42f15bc07cc3aa14f2cf5d0878781ac58775e11e5837cd"
    assert roots["abc"] == ABC_ROOT
    assert roots["abcc"] == ABC_ROOT  # the frozen ambiguity
    assert roots["five-mixed"] == "412b35586144b38c9d53e9b58184ac82c959e0fcf42fbb5f89ed4ce51c23bb95"
    assert roots["empty-sentinel"] == "62d32face721021db81a4f51cd92801934df71977a317ba6f5781b62b5792e8b"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"].split(" ")[0])
def test_library_reproduces_leaf_hashes_roots_and_proofs(case):
    leaves = _leaves(case)
    assert [l.hash_hex for l in leaves] == [l["leaf_hash"] for l in case["leaves"]]
    tree = MerkleTree(leaves)
    assert tree.root_hash == case["root"]
    assert tree.leaf_count == len(leaves)

    for item in case["proofs"]:
        index = int(item["index"])
        expected_steps = tuple(MerkleProofStep(sibling_hash=s["sibling_hash"], is_left=s["is_left"]) for s in item["steps"])
        # a proof built from the vector alone verifies through every entry point
        from_vector = MerkleProof(leaf=leaves[index], steps=expected_steps, root_hash=case["root"])
        assert from_vector.verify() is True
        assert from_vector.verify(expected_root=case["root"]) is True
        assert verify_proof_with_node_prefix(from_vector) is True
        # where account ids are unique, the library generates exactly the vector's proof
        ids = [l.account_id for l in leaves]
        if ids.count(leaves[index].account_id) == 1:
            assert tree.proof_for(leaves[index].account_id) == from_vector


def test_leaf_and_node_bytes_are_the_documented_ones():
    leaf = MerkleLeaf("alice", 300_000, "JPY")
    assert leaf.hash_hex == hashlib.sha256(b"LEAF:alice:300000:JPY").hexdigest()
    a, b = MerkleLeaf("alice", 300_000, "JPY"), MerkleLeaf("bob", 120_000, "JPY")
    expected = hashlib.sha256(b"NODE:" + bytes.fromhex(a.hash_hex) + bytes.fromhex(b.hash_hex)).hexdigest()
    assert MerkleTree([a, b]).root_hash == expected


def test_ledger_with_fixed_ids_still_gives_the_legacy_root():
    storage = SQLiteLedgerStorage(":memory:")
    ledger = Ledger(storage)
    for account_id, owner_type in (("platform", OwnerType.PLATFORM), ("alice", OwnerType.USER),
                                   ("bob", OwnerType.USER), ("carol", OwnerType.USER)):
        storage.save_account(Account(account_id=account_id, owner_type=owner_type, currency=Currency.JPY,
                                     label=account_id, created_at=T))
    for account_id, amount in (("alice", 300_000), ("bob", 120_000), ("carol", 55_000)):
        b = TransactionBuilder("FUND", "test", initiated_at=T)
        b.transfer("platform", account_id, Money(cents=amount, currency=Currency.JPY))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T)
    proof, tree = ProofOfReservesGenerator(ledger).generate(Currency.JPY, snapshot_at=T)
    assert tree.root_hash == ABC_ROOT
    assert proof.user_liabilities_root == ABC_ROOT
    assert proof.user_liabilities_total_cents == 475_000
    assert tree.proof_for("bob").verify(expected_root=ABC_ROOT) is True
    storage.close()


# ---------- one verifier ----------


def test_hash_pair_is_gone_and_there_is_one_routine():
    assert not hasattr(merkle_mod, "hash_pair")
    source = Path(merkle_mod.__file__).read_text(encoding="utf-8")
    assert source.count('b"NODE:"') == 1, "the node hash must be written in exactly one place"


def test_the_three_entry_points_agree_on_every_genuine_proof_for_1_to_33_leaves():
    checked = 0
    for n in range(1, 34):
        tree = _numbered_tree(n)
        for i in range(n):
            proof = tree.proof_for(f"acct-{i}")
            assert proof.verify() is True
            assert proof.verify(expected_root=tree.root_hash) is True
            assert verify_proof_with_node_prefix(proof) is True
            checked += 1
    assert checked == sum(range(1, 34)) == 561


def test_changed_proofs_are_rejected_by_every_entry_point():
    tree = _abc_tree()
    genuine = tree.proof_for("bob")
    assert len(genuine.steps) == 2 and genuine.verify(expected_root=ABC_ROOT)
    other_hash = "ab" * 32

    flipped = replace(genuine, steps=(replace(genuine.steps[0], is_left=not genuine.steps[0].is_left),) + genuine.steps[1:])
    replaced = replace(genuine, steps=(replace(genuine.steps[0], sibling_hash=other_hash),) + genuine.steps[1:])
    dropped = replace(genuine, steps=genuine.steps[:-1])
    added = replace(genuine, steps=genuine.steps + (MerkleProofStep(sibling_hash=other_hash, is_left=False),))
    other_leaf = replace(genuine, leaf=MerkleLeaf("alice", 300_000, "JPY"))
    other_amount = replace(genuine, leaf=replace(genuine.leaf, balance_cents=120_001))
    swapped = replace(genuine, steps=(genuine.steps[1], genuine.steps[0]))

    for changed in (flipped, replaced, dropped, added, other_leaf, other_amount, swapped):
        assert changed.verify() is False
        assert changed.verify(expected_root=ABC_ROOT) is False
        assert verify_proof_with_node_prefix(changed) is False


def test_wrong_expected_root_is_rejected():
    genuine = _abc_tree().proof_for("bob")
    assert genuine.verify(expected_root="00" * 32) is False
    assert genuine.verify(expected_root=_numbered_tree(3).root_hash) is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        "g" + "a" * 63,          # a non-hexadecimal character
        "AB" * 32,               # upper case
        "a" * 63,                # 63 characters
        "a" * 65,                # 65 characters
        "a" * 31 + " " + "a" * 32,  # a space inside
        "a" * 64 + "\n",
        " " + "a" * 63,
        "",
        None,
        5,
        b"a" * 64,
        bytes(32),
    ],
)
def test_malformed_hashes_give_false_not_an_exception(bad_hash):
    genuine = _abc_tree().proof_for("bob")
    as_sibling = replace(genuine, steps=(MerkleProofStep(sibling_hash=bad_hash, is_left=True),) + genuine.steps[1:])
    as_root = replace(genuine, root_hash=bad_hash)
    assert as_sibling.verify() is False
    assert as_sibling.verify(expected_root=ABC_ROOT) is False
    assert verify_proof_with_node_prefix(as_sibling) is False
    assert as_root.verify() is False
    assert verify_proof_with_node_prefix(as_root) is False
    if bad_hash is not None:  # None means "no expected root given"
        assert genuine.verify(expected_root=bad_hash) is False


def test_upper_case_form_of_the_true_root_is_not_accepted():
    genuine = _abc_tree().proof_for("bob")
    assert genuine.verify(expected_root=ABC_ROOT.upper()) is False
    assert replace(genuine, root_hash=ABC_ROOT.upper()).verify() is False


def test_upper_case_form_of_a_true_sibling_hash_is_not_accepted():
    # The bytes would be the right ones, so only the format rule can refuse this proof.
    genuine = _abc_tree().proof_for("bob")
    for position in range(len(genuine.steps)):
        steps = list(genuine.steps)
        upper = steps[position].sibling_hash.upper()
        assert upper != steps[position].sibling_hash and bytes.fromhex(upper) == bytes.fromhex(steps[position].sibling_hash)
        steps[position] = replace(steps[position], sibling_hash=upper)
        changed = replace(genuine, steps=tuple(steps))
        assert changed.verify() is False
        assert changed.verify(expected_root=ABC_ROOT) is False
        assert verify_proof_with_node_prefix(changed) is False


def test_sixty_five_steps_are_refused_even_when_they_hash_to_the_expected_root():
    genuine = _abc_tree().proof_for("bob")
    filler = MerkleProofStep(sibling_hash="cd" * 32, is_left=False)

    def folded(steps: int) -> str:
        current = genuine.leaf.hash_hex
        for _ in range(steps):
            current = hashlib.sha256(b"NODE:" + bytes.fromhex(current) + bytes.fromhex("cd" * 32)).hexdigest()
        return current

    sixty_four = replace(genuine, steps=(filler,) * 64, root_hash=folded(64))
    assert sixty_four.verify() is True and sixty_four.verify(expected_root=folded(64)) is True
    sixty_five = replace(genuine, steps=(filler,) * 65, root_hash=folded(65))
    assert sixty_five.verify() is False
    assert sixty_five.verify(expected_root=folded(65)) is False
    assert verify_proof_with_node_prefix(sixty_five) is False


@pytest.mark.parametrize("bad_flag", [1, 0, "True", None, "left"])
def test_is_left_that_is_not_a_bool_gives_false(bad_flag):
    genuine = _abc_tree().proof_for("bob")
    changed = replace(genuine, steps=(MerkleProofStep(sibling_hash=genuine.steps[0].sibling_hash, is_left=bad_flag),) + genuine.steps[1:])
    assert changed.verify() is False
    assert changed.verify(expected_root=ABC_ROOT) is False
    assert verify_proof_with_node_prefix(changed) is False


def test_more_than_64_steps_gives_false():
    genuine = _abc_tree().proof_for("bob")
    filler = MerkleProofStep(sibling_hash="cd" * 32, is_left=False)
    sixty_five = replace(genuine, steps=(filler,) * 65)
    assert sixty_five.verify() is False
    assert sixty_five.verify(expected_root=ABC_ROOT) is False
    assert verify_proof_with_node_prefix(sixty_five) is False
    # 64 steps are processed (and rejected only because the root does not match)
    sixty_four = replace(genuine, steps=(filler,) * 64)
    current = genuine.leaf.hash_hex
    for _ in range(64):
        current = hashlib.sha256(b"NODE:" + bytes.fromhex(current) + bytes.fromhex("cd" * 32)).hexdigest()
    assert sixty_four.verify(expected_root=current) is True
    assert sixty_four.verify() is False


@pytest.mark.parametrize("not_a_proof", [None, 5, "proof", {"leaf": 1}, []])
def test_objects_that_are_not_proofs_give_false(not_a_proof):
    assert verify_proof_with_node_prefix(not_a_proof) is False


def test_malformed_parts_give_false():
    genuine = _abc_tree().proof_for("bob")
    assert replace(genuine, steps=None).verify() is False
    assert replace(genuine, steps=("x",)).verify() is False
    assert replace(genuine, leaf="bob").verify() is False
    assert replace(genuine, leaf=MerkleLeaf("\ud800", 1, "JPY")).verify() is False


def test_replaced_own_root_verifies_only_if_it_is_the_true_root():
    genuine = _abc_tree().proof_for("carol")
    published = ABC_ROOT
    forged_root = "11" * 32
    with_forged_root = replace(genuine, root_hash=forged_root)
    assert with_forged_root.verify() is False
    assert replace(genuine, root_hash=published).verify() is True
    # a proof that is consistent with itself says nothing about the published root
    other_tree = _numbered_tree(3)
    foreign = other_tree.proof_for("acct-1")
    assert foreign.verify() is True
    assert foreign.verify(expected_root=published) is False
    # and verify(expected_root=...) ignores the root carried in the proof
    assert with_forged_root.verify(expected_root=published) is True
    assert with_forged_root.verify(expected_root=forged_root) is False
