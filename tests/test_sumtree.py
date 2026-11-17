"""The Merkle-sum tree, format openreserve.sumtree/1 (design section 6).

Expected values come from tests/vectors/sumtree_v1.json. The standalone script next to the
vectors (standard library only, nothing imported from the package) is used here as a second
implementation: it builds with the recursive definition and checks proofs with the loop of
RFC 9162, while the library builds level by level and derives sides from (index, count).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.transparency.sumtree as sumtree_mod
from openreserve.core.exact import AMOUNT_MAX, ExactValueError
from openreserve.transparency.sumtree import (
    DOMAIN,
    FORMAT_EXPORT,
    FORMAT_INCLUSION,
    KINDS,
    SumTree,
    SumTreeContext,
    SumTreeError,
    SumTreeLeaf,
    Verification,
    audit_sum_tree_export,
    derive_salt,
    verify_sum_tree_inclusion,
)

VECTOR_DIR = Path(__file__).parent / "vectors"
VECTORS = json.loads((VECTOR_DIR / "sumtree_v1.json").read_text(encoding="utf-8"))
SNAPSHOT_VECTORS = json.loads((VECTOR_DIR / "snapshot_expected_v1.json").read_text(encoding="utf-8"))
KEY = bytes.fromhex(VECTORS["salt_key_hex"])
TREES = {t["name"]: t for t in VECTORS["trees"]}

_spec = importlib.util.spec_from_file_location("check_vectors_standalone", VECTOR_DIR / "check_vectors_standalone.py")
standalone = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standalone)

CTX = SumTreeContext(
    kind="liabilities",
    institution_id="example-institution",
    currency="JPY",
    minor_units=0,
    as_of="2026-01-01T00:00:00.000000Z",
)
ABC = [("alice", 300_000), ("bob", 120_000), ("carol", 55_000)]
ABCD = ABC + [("dave", 9_000)]
OTHER_ROOT = "ab" * 32


def build(items, context=CTX, key=KEY) -> SumTree:
    return SumTree.build(context, items, salt_key=key)


def check(proof, root, slot) -> Verification:
    return verify_sum_tree_inclusion(proof, expected_root=root, expected_slot_id=slot)


def numbered(n: int) -> SumTree:
    return build([(f"acct-{i:03d}", 1000 + 7 * i) for i in range(n)])


# ---------------------------------------------------------------------------
# 1. The vectors
# ---------------------------------------------------------------------------


def test_vector_file_shape():
    assert VECTORS["format"] == "openreserve.sumtree/1"
    assert VECTORS["domain_ascii"].encode("ascii") == DOMAIN and len(DOMAIN) == 22
    assert [t["name"] for t in VECTORS["trees"]] == [
        "empty", "one-leaf", "two-leaves", "abc", "abc-plus-dave-with-carols-amount",
        "five", "seven", "eight", "nine-with-zero-and-large",
    ]
    assert sum(len(t["proofs"]) for t in VECTORS["trees"]) == 39


def test_pinned_roots_totals_and_counts():
    expected = {
        "empty": ("75ea0f466b6231dd9e548d87000a7e10d9d90d091a9ae692476a53ecbfc5b64a", 0, 0),
        "one-leaf": ("b122c6bf923cfe591ac253e3cfee5b1b03ba347132b5179620eda5d0bba5e1d4", 300000, 1),
        "two-leaves": ("1f19f1f192942e28c1c8be36910ae201dcc647cda2b75b6043cb3387720c027d", 420000, 2),
        "abc": ("15999a7b45465f066c26266c9c0371f430116f646c810dc30196623faba05b3a", 475000, 3),
        "abc-plus-dave-with-carols-amount": ("f88f652b794a454653c105b1c3aa965f3e0fd35c4bfab31df0b5ca46f4fc7757", 530000, 4),
        "five": ("7e1e7790e82274a3455a377b2b87f5fdef354cefbcd6e065e605b410d0eb6f99", 530001, 5),
        "seven": ("0e0020753f8dff41061a9754d117fe4ecb73e1c64f6dd1861c64ff57d8e8f066", 1111111, 7),
        "eight": ("c8af4e44904cecdb80007e85fb61184a31ebee60b7c9f44039c15b465a1ad158", 11111111, 8),
        "nine-with-zero-and-large": (
            "892f206b96e85b53009682877dd06a19c66c89f05a76c9cc59b9c10b50508379",
            10000001000000000009223372036854775831,
            9,
        ),
    }
    for name, (root, total, count) in expected.items():
        t = TREES[name]
        tree = build([(l["slot_id"], int(l["amount"])) for l in t["input_leaves"]], SumTreeContext.from_document(t["context"]))
        assert (tree.root, tree.total, tree.leaf_count) == (root, total, count), name
        assert t["head"] == {"root": root, "total": str(total), "leaf_count": str(count)}


@pytest.mark.parametrize("t", VECTORS["trees"], ids=lambda t: t["name"])
def test_vector_tree(t):
    context = SumTreeContext.from_document(t["context"])
    assert context.digest().hex() == t["context_digest"]
    assert context.to_document() == t["context"]
    items = [(l["slot_id"], int(l["amount"])) for l in t["input_leaves"]]
    tree = build(items, context)

    # salts, leaf hashes and order
    assert [
        {"amount": str(leaf.amount), "index": str(i), "leaf_hash": h, "salt": leaf.salt.hex(), "slot_id": leaf.slot_id}
        for i, (leaf, h) in enumerate(zip(tree.leaves, tree.leaf_hashes()))
    ] == t["canonical_leaves"]
    assert list(tree.leaf_hashes()) == sorted(tree.leaf_hashes())
    for leaf in tree.leaves:
        assert derive_salt(KEY, context, leaf.slot_id) == leaf.salt
        assert tree.index_of(leaf.slot_id) == tree.leaves.index(leaf)

    # top hash and head
    assert tree.top_hash == t["top_hash"]
    assert tree.head() == t["head"]
    assert tree.total == sum(amount for _, amount in items)

    # every proof
    assert len(t["proofs"]) == len(items)
    for item in t["proofs"]:
        proof = item["proof"]
        slot = proof["leaf"]["slot_id"]
        assert tree.proof_for(slot) == proof
        result = check(proof, t["head"]["root"], slot)
        assert result.ok is True and result.reason is None and bool(result) is True
        d = result.details
        assert set(d) == {"context", "leaf_count", "total", "slot_id", "amount", "payload", "disclosed"}
        assert d["context"] == t["context"]
        assert d["leaf_count"] == int(t["head"]["leaf_count"])
        assert d["total"] == int(t["head"]["total"])
        assert (d["slot_id"], d["amount"], d["payload"]) == (slot, int(proof["leaf"]["amount"]), "")
        assert [{"leaf_count": str(n), "sum": str(s)} for n, s in d["disclosed"]] == item["disclosed_sibling_subtrees"]
        assert sum(s for _, s in d["disclosed"]) == d["total"] - d["amount"]
        assert sum(n for n, _ in d["disclosed"]) == d["leaf_count"] - 1

    # the auditor export
    export = tree.export()
    assert [l["slot_id"] for l in export["leaves"]] == [c["slot_id"] for c in t["canonical_leaves"]]
    audit = audit_sum_tree_export(export, expected_root=t["head"]["root"])
    assert audit.ok is True
    assert audit.details["head"] == t["head"]
    assert (audit.details["root"], audit.details["total"], audit.details["leaf_count"]) == (
        t["head"]["root"], int(t["head"]["total"]), int(t["head"]["leaf_count"]),
    )


def test_byte_layout_example():
    ex = VECTORS["byte_layout_example"]
    tree = build(ABC[:2])
    assert tree.root == ex["root"] == "1f19f1f192942e28c1c8be36910ae201dcc647cda2b75b6043cb3387720c027d"
    assert tree.top_hash == ex["top_hash"] == "e718267f663d147859473a4adbfea0daf4e09c8b0a930a61587cac5024077084"
    assert CTX.digest().hex() == ex["context_digest"] == "ce298feaa458599c199eccf8c01e45c2e6ac6f890ba6df5cd2248e661ff0f002"
    assert [leaf.slot_id for leaf in tree.leaves] == ["bob", "alice"]
    assert tree.leaf_hashes() == (
        "cc2b75e2bd3b6c0fc552c792fc13535a1cbb9b3cf438338f5913bd1447c5f72c",
        "cd9c5265f875d3d411ea0b8fede88f72084669cee3029adedaf16fc43bc9e374",
    )
    assert derive_salt(KEY, CTX, "bob").hex() == ex["salt"] == "9718cd5fb3a5b136cf09d6cfcaafd38796fd2208558f5b6fa6f45ac966d632be"

    # the exact input bytes of each hash, rebuilt here from the written layout
    lp = lambda b: len(b).to_bytes(4, "big") + b
    u128 = lambda n: n.to_bytes(16, "big")
    context_preimage = (
        DOMAIN + b"\x03" + lp(b"liabilities") + lp(b"example-institution") + lp(b"JPY") + bytes([0])
        + lp(b"2026-01-01T00:00:00.000000Z") + lp(b"")
    )
    assert context_preimage.hex() == ex["context_preimage_hex"]
    ctx = hashlib.sha256(context_preimage).digest()
    salt_message = DOMAIN + b"\x10" + ctx + lp(b"bob")
    assert salt_message.hex() == ex["salt_hmac_message_hex"]
    salt_bob = hmac.new(KEY, salt_message, hashlib.sha256).digest()
    leaf_preimage = DOMAIN + b"\x00" + ctx + lp(b"bob") + u128(120000) + salt_bob + lp(b"")
    assert leaf_preimage.hex() == ex["leaf_preimage_hex"]
    leaf_bob = hashlib.sha256(leaf_preimage).digest()
    leaf_alice = bytes.fromhex(tree.leaf_hashes()[1])
    node_preimage = DOMAIN + b"\x01" + leaf_bob + u128(120000) + leaf_alice + u128(300000)
    assert node_preimage.hex() == ex["node_preimage_hex"] and len(node_preimage) == 119
    top = hashlib.sha256(node_preimage).digest()
    root_preimage = DOMAIN + b"\x02" + ctx + (2).to_bytes(8, "big") + u128(420000) + top
    assert root_preimage.hex() == ex["root_preimage_hex"] and len(root_preimage) == 111
    assert hashlib.sha256(root_preimage).hexdigest() == tree.root


def test_empty_tree_is_a_typed_special_case():
    tree = build([])
    assert tree.leaf_count == 0 and tree.total == 0
    assert tree.top_hash == "00" * 32
    assert tree.root == "75ea0f466b6231dd9e548d87000a7e10d9d90d091a9ae692476a53ecbfc5b64a"
    assert tree.head() == {"root": tree.root, "total": "0", "leaf_count": "0"}
    with pytest.raises(KeyError):
        tree.proof_for("alice")
    assert audit_sum_tree_export(tree.export(), expected_root=tree.root).ok
    # the empty root still binds the context
    receivables = SumTreeContext("receivables", "example-institution", "JPY", 0, "2026-01-01T00:00:00.000000Z")
    assert build([], receivables).root == "f7b3370b9761b2dcdbb71edfb057c32c7522b2ec5ac81665720716e2b5afd79c"


# ---------------------------------------------------------------------------
# 2. Totals and counts are bound to the root; nothing is padded or duplicated
# ---------------------------------------------------------------------------


def test_duplication_case_of_the_legacy_tree_cannot_be_expressed():
    with pytest.raises(SumTreeError):
        build(ABC + [("carol", 55_000)])
    with pytest.raises(SumTreeError):
        build(ABC + [("carol", 1)])
    abc = build(ABC)
    abcd = build(ABC + [("dave", 55_000)])
    assert (abc.total, abcd.total) == (475_000, 530_000)
    assert abc.root != abcd.root
    assert abc.root == "15999a7b45465f066c26266c9c0371f430116f646c810dc30196623faba05b3a"
    assert abcd.root == "f88f652b794a454653c105b1c3aa965f3e0fd35c4bfab31df0b5ca46f4fc7757"


def test_zero_amount_leaf_changes_the_root_although_the_total_is_equal():
    abc = build(ABC)
    padded = build(ABC + [("zero", 0)])
    assert padded.total == abc.total and padded.leaf_count == 4
    assert padded.root != abc.root
    assert padded.top_hash != abc.top_hash


def test_same_top_hash_with_another_count_or_total_is_another_root():
    abc = build(ABC)
    digest = CTX.digest()
    top = bytes.fromhex(abc.top_hash)
    assert sumtree_mod._root_hash(digest, 3, 475_000, top) == abc.root
    for count, total in ((4, 475_000), (2, 475_000), (3, 474_999), (3, 475_001), (3, 0)):
        assert sumtree_mod._root_hash(digest, count, total, top) != abc.root


def test_proof_cannot_be_presented_for_a_padded_count():
    tree = build(ABC)  # order: carol, bob, alice
    proof = tree.proof_for("alice")  # index 2 of 3: one step, the node (carol, bob)
    assert len(proof["path"]) == 1
    # as if the tree had four leaves, with a made-up zero leaf next to alice
    padded = copy.deepcopy(proof)
    padded["leaf_count"] = "4"
    padded["leaf_index"] = "2"
    padded["path"] = [{"hash": "00" * 32, "sum": "0"}] + proof["path"]
    assert check(padded, tree.root, "alice").reason == "ROOT_MISMATCH"
    # as if alice were paired with herself (the legacy rule for an odd last node)
    own_hash = tree.leaf_hashes()[2]
    doubled = copy.deepcopy(padded)
    doubled["path"][0] = {"hash": own_hash, "sum": "300000"}
    assert check(doubled, tree.root, "alice").reason == "ROOT_MISMATCH"
    doubled["path"][0] = {"hash": own_hash, "sum": "0"}
    assert check(doubled, tree.root, "alice").reason == "ROOT_MISMATCH"


# ---------------------------------------------------------------------------
# 3. Every field of the context, and the key, change the root
# ---------------------------------------------------------------------------


def test_each_context_field_changes_the_root():
    abc_root = build(ABC).root
    variants = VECTORS["abc_context_variants"]
    assert sorted(variants) == ["as_of_text", "binding", "currency", "institution_id", "kind", "minor_units"]
    roots = set()
    for name, v in variants.items():
        context = SumTreeContext.from_document(v["context"])
        assert context.digest().hex() == v["context_digest"], name
        tree = build(ABC, context)
        assert tree.root == v["root"], name
        assert tree.root != abc_root
        assert tree.total == 475_000 and tree.leaf_count == 3
        roots.add(tree.root)
    assert len(roots) == 6


def test_another_salt_key_changes_the_root():
    other = VECTORS["abc_root_with_another_salt_key"]
    tree = build(ABC, key=bytes.fromhex(other["salt_key_hex"]))
    assert tree.root == other["root"] == "8e284b9c62c12f170e2b1a08718a621342615e796e25ec074987acb2b3ff1486"
    assert tree.root != build(ABC).root
    assert tree.total == 475_000


def test_same_slot_and_amount_have_different_salts_and_leaf_hashes_in_another_context():
    later = SumTreeContext("liabilities", "example-institution", "JPY", 0, "2026-01-02T00:00:00.000000Z")
    a, b = build(ABC), build(ABC, later)
    for slot, _ in ABC:
        i, j = a.index_of(slot), b.index_of(slot)
        assert a.leaves[i].amount == b.leaves[j].amount
        assert a.leaves[i].salt != b.leaves[j].salt
        assert a.leaf_hashes()[i] != b.leaf_hashes()[j]
    assert set(a.leaf_hashes()).isdisjoint(b.leaf_hashes())


# ---------------------------------------------------------------------------
# 4. Building: order independence and refusals
# ---------------------------------------------------------------------------


def test_root_does_not_depend_on_input_order():
    rng = random.Random(20261117)
    items = [(f"acct-{i}", i * i + 1) for i in range(23)]
    reference = build(items)
    for _ in range(20):
        shuffled = items[:]
        rng.shuffle(shuffled)
        tree = build(shuffled)
        assert tree.root == reference.root
        assert tree.export() == reference.export()
        assert tree.proof_for("acct-7") == reference.proof_for("acct-7")


@pytest.mark.parametrize(
    "items",
    [
        [("alice", 1), ("alice", 1)],                    # duplicate slot id
        [("alice", 1), ("bob", 2), ("alice", 3)],        # duplicate slot id, another amount
        [("alice", -1)],                                  # negative amount
        [("alice", 10**38)],                              # above the bound
        [("alice", True)],                                # bool
        [("alice", 1.0)],                                 # float
        [("alice", "1")],                                 # text
        [("alice", None)],
        [("alice", AMOUNT_MAX), ("bob", 1)],              # total above the bound
        [("alice", AMOUNT_MAX // 2 + 1), ("bob", AMOUNT_MAX // 2 + 1)],
        [("alice", 1, "x")],                              # payload in a snapshot kind
        [("", 1)],                                        # empty slot id
        [("a" * 256, 1)],                                 # slot id too long
        [("line\nbreak", 1)],                             # control character
        [("del\x7f", 1)],
        [("\ud800", 1)],                                  # lone surrogate
        [(5, 1)],                                         # slot id not text
        [("alice",)],                                     # not a pair
        ["alice"],
    ],
)
def test_building_refuses(items):
    with pytest.raises(SumTreeError):
        build(items)


def test_amount_at_the_bound_is_accepted_and_one_more_unit_is_not():
    tree = build([("alice", AMOUNT_MAX)])
    assert tree.total == AMOUNT_MAX
    assert check(tree.proof_for("alice"), tree.root, "alice").details["total"] == AMOUNT_MAX
    tree = build([("alice", AMOUNT_MAX - 1), ("bob", 1), ("carol", 0)])
    assert tree.total == AMOUNT_MAX
    for slot in ("alice", "bob", "carol"):
        assert check(tree.proof_for(slot), tree.root, slot).ok


def test_leaves_with_explicit_salts_are_checked():
    salt = bytes(range(32))
    assert SumTree(CTX, [SumTreeLeaf("alice", 5, salt)]).total == 5
    for bad_salt in (b"", bytes(31), bytes(33), "00" * 32, None, bytearray(32)):
        with pytest.raises(SumTreeError):
            SumTree(CTX, [SumTreeLeaf("alice", 5, bad_salt)])
    with pytest.raises(SumTreeError):
        SumTree(CTX, [SumTreeLeaf("alice", 5, salt), SumTreeLeaf("alice", 5, salt)])
    with pytest.raises(SumTreeError):
        SumTree(CTX, [("alice", 5, salt)])
    with pytest.raises(SumTreeError):
        SumTree(CTX.to_document(), [])


@pytest.mark.parametrize("key", [b"", bytes(31), "k" * 32, None, bytearray(32), 5])
def test_salt_key_must_be_bytes_of_at_least_32(key):
    with pytest.raises(SumTreeError):
        SumTree.build(CTX, ABC, salt_key=key)
    with pytest.raises(SumTreeError):
        SumTree.build(CTX, [], salt_key=key)
    with pytest.raises(SumTreeError):
        derive_salt(key, CTX, "alice")
    assert issubclass(SumTreeError, ValueError) and issubclass(SumTreeError, ExactValueError)


def test_longer_salt_key_is_accepted():
    assert SumTree.build(CTX, ABC, salt_key=bytes(64)).total == 475_000


@pytest.mark.parametrize(
    "fields",
    [
        dict(kind="assets"),
        dict(kind=""),
        dict(kind=None),
        dict(institution_id=""),
        dict(institution_id="x" * 256),
        dict(institution_id="tab\there"),
        dict(institution_id=5),
        dict(currency="jpy"),
        dict(currency="1JPY"),
        dict(currency="JP-Y"),
        dict(currency="A" * 33),
        dict(currency=""),
        dict(minor_units=-1),
        dict(minor_units=31),
        dict(minor_units=True),
        dict(minor_units="0"),
        dict(minor_units=0.0),
        dict(as_of="2026-01-01T00:00:00Z"),
        dict(as_of="2026-01-01T00:00:00+00:00"),
        dict(as_of=None),
        dict(binding=bytes(31)),
        dict(binding=bytes(33)),
        dict(binding="00" * 32),
        dict(binding=None),
    ],
)
def test_context_refuses(fields):
    base = dict(kind="liabilities", institution_id="example-institution", currency="JPY", minor_units=0,
                as_of="2026-01-01T00:00:00.000000Z", binding=b"")
    base.update(fields)
    with pytest.raises(SumTreeError):
        SumTreeContext(**base)


def test_context_accepts_the_documented_ranges():
    for kind in KINDS:
        assert SumTreeContext(kind, "i", "A", 0, "2026-01-01T00:00:00.000000Z").kind == kind
    wide = SumTreeContext("liabilities", "é" * 127, "A" + "Z9_" * 10 + "Z", 30, "9999-12-31T23:59:59.999999Z", bytes(32))
    assert SumTreeContext.from_document(wide.to_document()) == wide
    assert wide.to_document()["minor_units"] == "30" and wide.to_document()["binding"] == "00" * 32


def test_payload_is_bound_in_conversion_kinds():
    context = SumTreeContext("conversion-liabilities", "example-institution", "JPY", 0,
                             "2026-01-01T00:00:00.000000Z", bytes.fromhex("11" * 32))
    a = build([("alice", 5, '{"k":"1"}')], context)
    b = build([("alice", 5, '{"k":"2"}')], context)
    assert a.root != b.root
    proof = a.proof_for("alice")
    assert check(proof, a.root, "alice").details["payload"] == '{"k":"1"}'
    changed = copy.deepcopy(proof)
    changed["leaf"]["payload"] = '{"k":"2"}'
    assert check(changed, a.root, "alice").reason == "ROOT_MISMATCH"
    with pytest.raises(SumTreeError):
        build([("alice", 5, "x" * 4097)], context)
    assert build([("alice", 5, "x" * 4096)], context).total == 5


# ---------------------------------------------------------------------------
# 5. Tampering with a genuine proof: exact reasons
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def five():
    tree = build(ABC + [("dave", 55_000), ("erin", 1)])
    assert tree.root == "7e1e7790e82274a3455a377b2b87f5fdef354cefbcd6e065e605b410d0eb6f99"
    slot = tree.leaves[1].slot_id  # index 1 of 5: a path of three steps
    proof = tree.proof_for(slot)
    assert len(proof["path"]) == 3
    assert check(proof, tree.root, slot).ok
    return tree, slot, proof


def _mutated(proof, fn):
    changed = copy.deepcopy(proof)
    fn(changed)
    return changed


def _flip_char(text: str, position: int = 0) -> str:
    replacement = "0" if text[position] != "0" else "1"
    return text[:position] + replacement + text[position + 1:]


ROOT_MISMATCH_CHANGES = {
    "amount plus 1": lambda p: p["leaf"].__setitem__("amount", str(int(p["leaf"]["amount"]) + 1)),
    "amount minus 1": lambda p: p["leaf"].__setitem__("amount", str(int(p["leaf"]["amount"]) - 1)),
    "other slot id": lambda p: p["leaf"].__setitem__("slot_id", "mallory"),
    "one salt character": lambda p: p["leaf"].__setitem__("salt", _flip_char(p["leaf"]["salt"], 5)),
    "one sibling hash character": lambda p: p["path"][1].__setitem__("hash", _flip_char(p["path"][1]["hash"], 63)),
    "first sibling sum plus 1": lambda p: p["path"][0].__setitem__("sum", str(int(p["path"][0]["sum"]) + 1)),
    "first sibling sum minus 1": lambda p: p["path"][0].__setitem__("sum", str(int(p["path"][0]["sum"]) - 1)),
    "last sibling sum plus 1": lambda p: p["path"][-1].__setitem__("sum", str(int(p["path"][-1]["sum"]) + 1)),
    "last sibling sum minus 1": lambda p: p["path"][-1].__setitem__("sum", str(int(p["path"][-1]["sum"]) - 1)),
    "sum moved between two siblings": lambda p: (
        p["path"][0].__setitem__("sum", str(int(p["path"][0]["sum"]) - 1)),
        p["path"][1].__setitem__("sum", str(int(p["path"][1]["sum"]) + 1)),
    ),
    "two path steps swapped": lambda p: p["path"].__setitem__(slice(0, 2), [p["path"][1], p["path"][0]]),
    "sibling sums swapped, hashes kept": lambda p: (
        lambda a, b: (p["path"][0].__setitem__("sum", b), p["path"][1].__setitem__("sum", a))
    )(p["path"][0]["sum"], p["path"][1]["sum"]),
    "kind": lambda p: p["context"].__setitem__("kind", "receivables"),
    "institution": lambda p: p["context"].__setitem__("institution_id", "other-institution"),
    "currency": lambda p: p["context"].__setitem__("currency", "USD"),
    "minor units": lambda p: p["context"].__setitem__("minor_units", "2"),
    "instant": lambda p: p["context"].__setitem__("as_of", "2026-01-01T00:00:00.000001Z"),
    "binding": lambda p: p["context"].__setitem__("binding", "11" * 32),
    "leaf index of the neighbour": lambda p: p.__setitem__("leaf_index", "0"),
}


@pytest.mark.parametrize("name", sorted(ROOT_MISMATCH_CHANGES))
def test_tampering_gives_root_mismatch(five, name):
    tree, slot, proof = five
    changed = _mutated(proof, ROOT_MISMATCH_CHANGES[name])
    assert changed != proof
    result = check(changed, tree.root, slot if name != "other slot id" else "mallory")
    assert (result.ok, result.reason) == (False, "ROOT_MISMATCH")
    assert bool(result) is False


def test_tampering_with_the_path_length(five):
    tree, slot, proof = five
    removed = _mutated(proof, lambda p: p["path"].pop())
    appended = _mutated(proof, lambda p: p["path"].append({"hash": "00" * 32, "sum": "0"}))
    emptied = _mutated(proof, lambda p: p["path"].clear())
    for changed in (removed, appended, emptied):
        assert check(changed, tree.root, slot).reason == "PATH_LENGTH_MISMATCH"


def test_sibling_sum_that_overflows_the_running_sum(five):
    tree, slot, proof = five
    assert int(proof["leaf"]["amount"]) >= 1
    for step in range(3):
        changed = _mutated(proof, lambda p: p["path"][step].__setitem__("sum", str(AMOUNT_MAX)))
        assert check(changed, tree.root, slot).reason == "SUM_OUT_OF_RANGE"
    # the boundary, on a one-step path: a running sum of exactly 10**38 - 1 is in range (and
    # the proof then fails on the root); one more unit is out of range
    two = build([("alice", 300_000), ("bob", 120_000)])
    genuine = two.proof_for("alice")
    assert len(genuine["path"]) == 1
    at_bound = _mutated(genuine, lambda p: p["path"][0].__setitem__("sum", str(AMOUNT_MAX - 300_000)))
    assert check(at_bound, two.root, "alice").reason == "ROOT_MISMATCH"
    above = _mutated(genuine, lambda p: p["path"][0].__setitem__("sum", str(AMOUNT_MAX - 300_000 + 1)))
    assert check(above, two.root, "alice").reason == "SUM_OUT_OF_RANGE"
    zero_amount = build([("zero", 0), ("bob", 1)])
    p0 = zero_amount.proof_for("zero")
    p0["path"][0]["sum"] = str(AMOUNT_MAX)  # running sum is 0: the addition stays in range
    assert check(p0, zero_amount.root, "zero").reason == "ROOT_MISMATCH"


MALFORMED_CHANGES = {
    "sibling sum -1": lambda p: p["path"][0].__setitem__("sum", "-1"),
    "sibling sum with a space": lambda p: p["path"][0].__setitem__("sum", " 5"),
    "sibling sum 05": lambda p: p["path"][0].__setitem__("sum", "05"),
    "sibling sum as the number 5": lambda p: p["path"][0].__setitem__("sum", 5),
    "sibling sum 10**38": lambda p: p["path"][0].__setitem__("sum", str(10**38)),
    "sibling sum empty": lambda p: p["path"][0].__setitem__("sum", ""),
    "sibling sum null": lambda p: p["path"][0].__setitem__("sum", None),
    "hash in upper case": lambda p: p["path"][0].__setitem__("hash", p["path"][0]["hash"].upper()),
    "hash with a space": lambda p: p["path"][0].__setitem__("hash", p["path"][0]["hash"][:31] + " " + p["path"][0]["hash"][32:]),
    "hash of 63 characters": lambda p: p["path"][0].__setitem__("hash", p["path"][0]["hash"][:63]),
    "hash of 65 characters": lambda p: p["path"][0].__setitem__("hash", p["path"][0]["hash"] + "0"),
    "hash null": lambda p: p["path"][0].__setitem__("hash", None),
    "extra key": lambda p: p.__setitem__("note", "x"),
    "extra key in leaf": lambda p: p["leaf"].__setitem__("note", "x"),
    "extra key in context": lambda p: p["context"].__setitem__("note", "x"),
    "extra key in a path element": lambda p: p["path"][0].__setitem__("side", "L"),
    "missing key leaf_index": lambda p: p.pop("leaf_index"),
    "missing key path": lambda p: p.pop("path"),
    "missing salt": lambda p: p["leaf"].pop("salt"),
    "missing binding": lambda p: p["context"].pop("binding"),
    "missing sum": lambda p: p["path"][0].pop("sum"),
    "path not a list": lambda p: p.__setitem__("path", {"0": p["path"][0]}),
    "path null": lambda p: p.__setitem__("path", None),
    "path element not an object": lambda p: p["path"].__setitem__(0, "x"),
    "payload x in a snapshot proof": lambda p: p["leaf"].__setitem__("payload", "x"),
    "payload null": lambda p: p["leaf"].__setitem__("payload", None),
    "amount -1": lambda p: p["leaf"].__setitem__("amount", "-1"),
    "amount as a number": lambda p: p["leaf"].__setitem__("amount", 120000),
    "amount 10**38": lambda p: p["leaf"].__setitem__("amount", str(10**38)),
    "amount with a plus sign": lambda p: p["leaf"].__setitem__("amount", "+1"),
    "salt in upper case": lambda p: p["leaf"].__setitem__("salt", p["leaf"]["salt"].upper()),
    "salt too short": lambda p: p["leaf"].__setitem__("salt", p["leaf"]["salt"][:62]),
    "slot id empty": lambda p: p["leaf"].__setitem__("slot_id", ""),
    "slot id with a control character": lambda p: p["leaf"].__setitem__("slot_id", "a\x00b"),
    "slot id as a number": lambda p: p["leaf"].__setitem__("slot_id", 5),
    "leaf not an object": lambda p: p.__setitem__("leaf", [p["leaf"]]),
    "leaf_count as a number": lambda p: p.__setitem__("leaf_count", 5),
    "leaf_count 05": lambda p: p.__setitem__("leaf_count", "05"),
    "leaf_count -5": lambda p: p.__setitem__("leaf_count", "-5"),
    "leaf_count 2**64": lambda p: p.__setitem__("leaf_count", str(2**64)),
    "leaf_index as a number": lambda p: p.__setitem__("leaf_index", 1),
    "leaf_index with a newline": lambda p: p.__setitem__("leaf_index", "1\n"),
    "unknown kind": lambda p: p["context"].__setitem__("kind", "assets"),
    "currency in lower case": lambda p: p["context"].__setitem__("currency", "jpy"),
    "minor units 31": lambda p: p["context"].__setitem__("minor_units", "31"),
    "minor units as a number": lambda p: p["context"].__setitem__("minor_units", 0),
    "instant without microseconds": lambda p: p["context"].__setitem__("as_of", "2026-01-01T00:00:00Z"),
    "binding in upper case": lambda p: p["context"].__setitem__("binding", "AB" * 32),
    "binding of 62 characters": lambda p: p["context"].__setitem__("binding", "ab" * 31),
    "context not an object": lambda p: p.__setitem__("context", "liabilities"),
    "a float anywhere": lambda p: p["leaf"].__setitem__("amount", 1.5),
    "a key outside the profile": lambda p: p.__setitem__("Format", "x"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_CHANGES))
def test_tampering_gives_malformed_proof(five, name):
    tree, slot, proof = five
    changed = _mutated(proof, MALFORMED_CHANGES[name])
    assert changed != proof
    result = check(changed, tree.root, slot)
    assert (result.ok, result.reason) == (False, "MALFORMED_PROOF")


@pytest.mark.parametrize(
    "document",
    [None, 5, "proof", [], [1], b"{}", {}, {"format": None}, {"format": "openreserve.sumtree.inclusion/2"},
     {"format": "openreserve.sumtree.export/1"}, {"format": ["openreserve.sumtree.inclusion/1"]}],
)
def test_other_documents_are_unsupported_format(five, document):
    tree, slot, _proof = five
    assert check(document, tree.root, slot).reason == "UNSUPPORTED_FORMAT"


def test_another_format_string_on_a_genuine_proof(five):
    tree, slot, proof = five
    changed = _mutated(proof, lambda p: p.__setitem__("format", "openreserve.sumtree.inclusion/2"))
    assert check(changed, tree.root, slot).reason == "UNSUPPORTED_FORMAT"
    removed = _mutated(proof, lambda p: p.pop("format"))
    assert check(removed, tree.root, slot).reason == "UNSUPPORTED_FORMAT"


def test_genuine_proof_with_another_expected_slot_or_root(five):
    tree, slot, proof = five
    other_slot = tree.leaves[0].slot_id
    assert check(proof, tree.root, other_slot).reason == "SLOT_MISMATCH"
    assert check(proof, OTHER_ROOT, slot).reason == "ROOT_MISMATCH"
    assert check(proof, build(ABC).root, slot).reason == "ROOT_MISMATCH"
    # the root is checked before the slot
    assert check(proof, OTHER_ROOT, other_slot).reason == "ROOT_MISMATCH"


def test_count_and_index_reasons(five):
    tree, slot, proof = five
    assert check(_mutated(proof, lambda p: p.__setitem__("leaf_count", "0")), tree.root, slot).reason == "EMPTY_TREE"
    assert check(_mutated(proof, lambda p: p.__setitem__("leaf_index", "5")), tree.root, slot).reason == "INDEX_OUT_OF_RANGE"
    assert check(_mutated(proof, lambda p: p.__setitem__("leaf_index", str(2**64))), tree.root, slot).reason == "INDEX_OUT_OF_RANGE"
    huge = _mutated(proof, lambda p: (p.__setitem__("leaf_count", str(2**64 - 1)), p.__setitem__("leaf_index", str(2**64 - 2))))
    assert check(huge, tree.root, slot).reason == "PATH_LENGTH_MISMATCH"


@pytest.mark.parametrize("bad_root", ["", "ab" * 31, "AB" * 32, "ab" * 32 + "\n", None, 5, bytes(32)])
def test_malformed_expected_root_is_a_programming_error(five, bad_root):
    tree, slot, proof = five
    with pytest.raises(ValueError):
        verify_sum_tree_inclusion(proof, expected_root=bad_root, expected_slot_id=slot)
    with pytest.raises(ValueError):
        audit_sum_tree_export(tree.export(), expected_root=bad_root)


@pytest.mark.parametrize("bad_slot", ["", None, 5, "a\nb", "x" * 256])
def test_malformed_expected_slot_is_a_programming_error(five, bad_slot):
    tree, _slot, proof = five
    with pytest.raises(ValueError):
        verify_sum_tree_inclusion(proof, expected_root=tree.root, expected_slot_id=bad_slot)


def test_proof_contains_no_root_and_no_other_identifier(five):
    tree, slot, proof = five
    text = json.dumps(proof)
    assert tree.root not in text
    for leaf in tree.leaves:
        if leaf.slot_id != slot:
            assert leaf.slot_id not in text
            assert leaf.salt.hex() not in text
    assert json.loads(json.dumps(proof)) == proof  # plain JSON, strings only
    assert "1.0" not in text


# ---------------------------------------------------------------------------
# 6. No other (index, count) is accepted
# ---------------------------------------------------------------------------


def test_no_other_index_or_count_is_accepted_for_trees_of_1_to_40_leaves():
    variants = accepted = 0
    reasons: dict[str, int] = {}
    for n in range(1, 41):
        tree = numbered(n)
        for true_index, leaf in enumerate(tree.leaves):
            proof = tree.proof_for(leaf.slot_id)
            assert check(proof, tree.root, leaf.slot_id).ok
            for count in range(max(0, n - 2), n + 3):
                for index in range(0, count + 2):
                    if (index, count) == (true_index, n):
                        continue
                    changed = dict(proof)
                    changed["leaf_index"] = str(index)
                    changed["leaf_count"] = str(count)
                    result = check(changed, tree.root, leaf.slot_id)
                    variants += 1
                    accepted += result.ok
                    reasons[result.reason] = reasons.get(result.reason, 0) + 1
    assert accepted == 0
    assert variants == 118_079
    assert set(reasons) == {"EMPTY_TREE", "INDEX_OUT_OF_RANGE", "PATH_LENGTH_MISMATCH", "ROOT_MISMATCH"}
    assert sum(reasons.values()) == variants


# ---------------------------------------------------------------------------
# 7. Agreement with the standalone implementation
# ---------------------------------------------------------------------------


def _standalone_path(index: int, nodes: list) -> list:
    """Audit path by the recursive definition of RFC 6962 section 2.1.1, carrying sums."""
    n = len(nodes)
    if n == 1:
        return []
    k = 1
    while 2 * k < n:
        k *= 2
    if index < k:
        return _standalone_path(index, nodes[:k]) + [standalone.top(nodes[k:])]
    return _standalone_path(index - k, nodes[k:]) + [standalone.top(nodes[:k])]


def test_library_and_standalone_implementation_agree_for_trees_of_1_to_130_leaves():
    context_document = CTX.to_document()
    ctx = standalone.context_digest(context_document)
    assert ctx == CTX.digest()
    pairs = 0
    for n in range(1, 131):
        items = [(f"acct-{i:03d}", (i * 7919) % 100_003) for i in range(n)]
        tree = build(items)
        rows = []
        for slot, amount in items:
            salt = standalone.salt_for(KEY, ctx, slot)
            rows.append((standalone.leaf_hash(ctx, slot, amount, salt), slot, amount, salt))
        rows.sort()
        nodes = [(r[0], r[2]) for r in rows]
        top_hash, total = standalone.top(nodes)
        assert standalone.root(ctx, n, total, top_hash) == tree.root
        assert [r[1] for r in rows] == [leaf.slot_id for leaf in tree.leaves]
        for index, row in enumerate(rows):
            # the standalone proof equals the library's proof
            expected_path = [{"hash": h.hex(), "sum": str(s)} for h, s in _standalone_path(index, nodes)]
            proof = tree.proof_for(row[1])
            assert proof["path"] == expected_path
            assert (proof["leaf_index"], proof["leaf_count"]) == (str(index), str(n))
            # the library's proof verifies in the standalone verifier
            folded = standalone.fold(row[0], row[2], index, n, [(bytes.fromhex(s["hash"]), int(s["sum"])) for s in proof["path"]])
            assert folded is not None and standalone.root(ctx, n, folded[1], folded[0]) == tree.root
            # and in the library's
            assert check(proof, tree.root, row[1]).ok
            pairs += 1
    assert pairs == 8515


def test_sibling_plan_matches_the_definition():
    def by_definition(i, n):
        out = []
        while n > 1:
            k = 1
            while 2 * k < n:
                k *= 2
            if i < k:
                out.append(("R", n - k))
                n = k
            else:
                out.append(("L", k))
                i -= k
                n -= k
        return list(reversed(out))

    for n in range(1, 200):
        for i in range(n):
            plan = sumtree_mod._sibling_plan(i, n)
            assert plan == by_definition(i, n)
            assert sum(size for _, size in plan) == n - 1
    assert sumtree_mod._sibling_plan(0, 1) == []
    assert sumtree_mod._sibling_plan(2, 3) == [("L", 2)]
    assert sumtree_mod._sibling_plan(3, 4) == [("L", 1), ("L", 2)]
    assert sumtree_mod._sibling_plan(4, 5) == [("L", 4)]
    assert len(sumtree_mod._sibling_plan(0, 2**64 - 1)) == 64
    # the last leaf of 2**64 - 1 leaves: one step per set bit of the count, minus one
    assert len(sumtree_mod._sibling_plan(2**64 - 2, 2**64 - 1)) == 63
    assert sumtree_mod._sibling_plan(2**63, 2**63 + 1) == [("L", 2**63)]


# ---------------------------------------------------------------------------
# 8. The counter-example against a sum-only node hash
# ---------------------------------------------------------------------------


def test_sum_only_tree_accepts_an_understated_total_and_this_tree_does_not():
    balances = [("u1", 1), ("u2", 2), ("u3", 3), ("u4", 4)]

    # --- a sum-only construction: a node hashes its own value and the two child hashes ---
    def h(*parts: bytes) -> bytes:
        return hashlib.sha256(b"|".join(parts)).digest()

    def sum_only_node(value: int, left_hash: bytes, right_hash: bytes) -> bytes:
        return h(b"node", str(value).encode(), left_hash, right_hash)

    def sum_only_verify(leaf_hash, amount, path, root_value, root_hash) -> bool:
        current_hash, current_value = leaf_hash, amount
        for side, sibling_hash, sibling_value in path:
            if sibling_value < 0:
                return False
            current_value += sibling_value
            current_hash = (
                sum_only_node(current_value, current_hash, sibling_hash) if side == "R"
                else sum_only_node(current_value, sibling_hash, current_hash)
            )
        return (current_value, current_hash) == (root_value, root_hash)

    leaf = [h(b"leaf", slot.encode(), str(amount).encode()) for slot, amount in balances]
    # The prover sets every node to the larger child instead of the sum.
    node_12 = sum_only_node(2, leaf[0], leaf[1])
    node_34 = sum_only_node(4, leaf[2], leaf[3])
    root_hash = sum_only_node(4, node_12, node_34)
    forged_paths = [
        [("R", leaf[1], 1), ("R", node_34, 2)],  # u1: 1 + 1 = 2, 2 + 2 = 4
        [("L", leaf[0], 0), ("R", node_34, 2)],  # u2: 2 + 0 = 2, 2 + 2 = 4
        [("R", leaf[3], 1), ("L", node_12, 0)],  # u3: 3 + 1 = 4, 4 + 0 = 4
        [("L", leaf[2], 0), ("L", node_12, 0)],  # u4: 4 + 0 = 4, 4 + 0 = 4
    ]
    for (slot, amount), leaf_hash, path in zip(balances, leaf, forged_paths):
        assert sum_only_verify(leaf_hash, amount, path, 4, root_hash) is True
    assert sum(amount for _, amount in balances) == 10  # while every holder accepted a total of 4

    # --- the same forged sibling sums against this tree ---
    tree = build(balances)
    assert tree.total == 10
    forged_roots = set()
    for slot, amount in balances:
        genuine = tree.proof_for(slot)
        assert check(genuine, tree.root, slot).details["total"] == 10
        index = int(genuine["leaf_index"])
        # the same trick: show sibling sums that make the running sum the larger child (at most 4)
        forged = copy.deepcopy(genuine)
        running = amount
        for step in forged["path"]:
            shown = max(0, min(int(step["sum"]), 4 - running))
            step["sum"] = str(shown)
            running += shown
        assert running <= 4 < 10
        assert forged != genuine
        assert check(forged, tree.root, slot).reason == "ROOT_MISMATCH"
        # what root would this forged proof lead to? (computed with the standalone code)
        ctx = CTX.digest()
        leaf_hash = bytes.fromhex(tree.leaf_hashes()[index])
        folded = standalone.fold(leaf_hash, amount, index, 4, [(bytes.fromhex(s["hash"]), int(s["sum"])) for s in forged["path"]])
        forged_root = standalone.root(ctx, 4, folded[1], folded[0])
        assert check(forged, forged_root, slot).ok  # consistent with itself ...
        forged_roots.add(forged_root)
    # ... but no single root satisfies two holders: each forged proof leads to its own root,
    # because a node hash binds both child sums.
    assert len(forged_roots) == 4 and tree.root not in forged_roots


def test_accepted_proofs_give_a_lower_bound_on_the_total():
    rng = random.Random(6_6)
    for n in (1, 2, 3, 5, 8, 13, 21):
        items = [(f"acct-{i}", rng.randrange(0, 10**12)) for i in range(n)]
        tree = build(items)
        proven = 0
        for slot, amount in items:
            result = check(tree.proof_for(slot), tree.root, slot)
            assert result.ok and result.details["amount"] == amount and result.details["total"] == tree.total
            proven += result.details["amount"]
        assert proven == tree.total == sum(amount for _, amount in items)


# ---------------------------------------------------------------------------
# 9. What a proof discloses (pinned)
# ---------------------------------------------------------------------------


def test_pinned_disclosure_example():
    tree = build(ABCD)
    assert tree.root == "0795a28fdc881a5065a683e173fabd140bec8da2a2d71479286bfed415944e79"
    assert tree.head() == SNAPSHOT_VECTORS["four_leaf_abcd"]
    assert [leaf.slot_id for leaf in tree.leaves] == SNAPSHOT_VECTORS["four_leaf_order"] == ["carol", "dave", "bob", "alice"]
    proof = tree.proof_for("alice")
    assert proof["leaf_index"] == SNAPSHOT_VECTORS["four_leaf_alice_index"] == "3"
    result = check(proof, tree.root, "alice")
    assert result.details["disclosed"] == ((1, 120_000), (2, 64_000))  # bob's amount; carol plus dave
    assert [list(pair) for pair in result.details["disclosed"]] == SNAPSHOT_VECTORS["four_leaf_alice_disclosed"]
    # the proof holds bob's leaf hash and exact amount, but not his identifier or salt
    assert proof["path"][0] == {"hash": tree.leaf_hashes()[2], "sum": "120000"}
    text = json.dumps(proof)
    for other in ("bob", "carol", "dave"):
        assert other not in text
        assert tree.leaves[tree.index_of(other)].salt.hex() not in text


def test_sibling_leaf_hash_cannot_be_reproduced_without_its_salt():
    tree = build(ABCD)
    proof = tree.proof_for("alice")
    bob_leaf_hash = proof["path"][0]["hash"]
    digest = CTX.digest()
    available_salts = [bytes(32), bytes.fromhex(proof["leaf"]["salt"])]  # all-zero, and alice's own
    lp = lambda b: len(b).to_bytes(4, "big") + b
    hits = 0
    for salt in available_salts:
        for guess in range(119_000, 121_001):  # the true amount, 120000, is in the range
            candidate = hashlib.sha256(
                DOMAIN + b"\x00" + digest + lp(b"bob") + guess.to_bytes(16, "big") + salt + lp(b"")
            ).hexdigest()
            hits += candidate == bob_leaf_hash
    assert hits == 0
    # with bob's salt the same loop does find it: the salt is what the guess lacks
    bob_salt = tree.leaves[tree.index_of("bob")].salt
    with_salt = hashlib.sha256(
        DOMAIN + b"\x00" + digest + lp(b"bob") + (120_000).to_bytes(16, "big") + bob_salt + lp(b"")
    ).hexdigest()
    assert with_salt == bob_leaf_hash


# ---------------------------------------------------------------------------
# Auditor export
# ---------------------------------------------------------------------------


def test_export_audit_reasons():
    tree = build(ABCD)
    export = tree.export()
    assert set(export) == {"format", "context", "leaves"} and export["format"] == FORMAT_EXPORT
    assert audit_sum_tree_export(export, expected_root=tree.root).ok
    assert audit_sum_tree_export(export, expected_root=OTHER_ROOT).reason == "ROOT_MISMATCH"

    def changed(fn):
        e = copy.deepcopy(export)
        fn(e)
        return audit_sum_tree_export(e, expected_root=tree.root).reason

    assert changed(lambda e: e["leaves"][0].__setitem__("amount", str(int(e["leaves"][0]["amount"]) + 1))) == "ROOT_MISMATCH"
    assert changed(lambda e: e["leaves"][0].__setitem__("slot_id", "mallory")) == "ROOT_MISMATCH"
    assert changed(lambda e: e["leaves"][0].__setitem__("salt", "00" * 32)) == "ROOT_MISMATCH"
    assert changed(lambda e: e["leaves"].pop()) == "ROOT_MISMATCH"
    assert changed(lambda e: e["leaves"].append({"slot_id": "zero", "amount": "0", "salt": "00" * 32, "payload": ""})) == "ROOT_MISMATCH"
    assert changed(lambda e: e["context"].__setitem__("currency", "USD")) == "ROOT_MISMATCH"
    assert changed(lambda e: e["leaves"].reverse()) == "NOT_IN_CANONICAL_ORDER"
    assert changed(lambda e: e["leaves"].__setitem__(slice(0, 2), [e["leaves"][1], e["leaves"][0]])) == "NOT_IN_CANONICAL_ORDER"
    assert changed(lambda e: e["leaves"].append(dict(e["leaves"][0]))) == "MALFORMED_EXPORT"  # duplicate slot
    assert changed(lambda e: e["leaves"][0].__setitem__("amount", "-1")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["leaves"][0].__setitem__("amount", str(10**38))) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["leaves"][0].__setitem__("amount", 5)) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["leaves"][0].__setitem__("amount", str(AMOUNT_MAX))) == "MALFORMED_EXPORT"  # total out of range
    assert changed(lambda e: e["leaves"][0].__setitem__("payload", "x")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["leaves"][0].pop("salt")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["leaves"][0].__setitem__("extra", "x")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e.__setitem__("leaves", None)) == "MALFORMED_EXPORT"
    assert changed(lambda e: e.__setitem__("extra", "x")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e.pop("context")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e["context"].__setitem__("kind", "assets")) == "MALFORMED_EXPORT"
    assert changed(lambda e: e.__setitem__("format", FORMAT_INCLUSION)) == "UNSUPPORTED_FORMAT"
    assert changed(lambda e: e.pop("format")) == "UNSUPPORTED_FORMAT"
    for document in (None, 5, [], "x", {}):
        assert audit_sum_tree_export(document, expected_root=tree.root).reason == "UNSUPPORTED_FORMAT"


def test_export_of_a_tree_is_not_accepted_as_a_proof_and_vice_versa():
    tree = build(ABC)
    assert check(tree.export(), tree.root, "alice").reason == "UNSUPPORTED_FORMAT"
    assert audit_sum_tree_export(tree.proof_for("alice"), expected_root=tree.root).reason == "UNSUPPORTED_FORMAT"


# ---------------------------------------------------------------------------
# 10. Seeded mutation test
# ---------------------------------------------------------------------------

ODD_VALUES = [
    None, True, False, 0, 1, -1, 5, 1.5, float("nan"), "", " ", "0", "1", "-1", "01", "1.0", "1e3",
    str(10**38), str(AMOUNT_MAX), str(2**64), "00" * 32, "ff" * 32, "AB" * 32, "ab" * 31, "x" * 64,
    [], [None], {}, {"a": "b"}, "liabilities", "receivables", "conversion-liabilities",
    "openreserve.sumtree.inclusion/1", "openreserve.sumtree.export/1", "\ud800", "a\x00b",
    "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00Z", b"bytes", ("tuple",),
]


def _paths(node, prefix=()):
    """Every (container, key) position in a nested document."""
    found = []
    if isinstance(node, dict):
        for key in list(node):
            found.append((node, key))
            found.extend(_paths(node[key]))
    elif isinstance(node, list):
        for index in range(len(node)):
            found.append((node, index))
            found.extend(_paths(node[index]))
    return found


def mutate(document, rng: random.Random):
    """One random change: replace a value, drop a key, add a key, or change one character."""
    changed = copy.deepcopy(document)
    positions = _paths(changed)
    kind = rng.choice(("replace", "replace", "drop", "add", "char", "char"))
    if kind == "replace":
        container, key = rng.choice(positions)
        container[key] = copy.deepcopy(rng.choice(ODD_VALUES))
    elif kind == "drop":
        container, key = rng.choice(positions)
        if isinstance(container, dict):
            del container[key]
        else:
            container.pop(key)
    elif kind == "add":
        containers = [changed] + [c[k] for c, k in positions if isinstance(c[k], (dict, list))]
        target = rng.choice(containers)
        if isinstance(target, dict):
            target[rng.choice(("extra", "note", "sum", "hash", "root", "x"))] = copy.deepcopy(rng.choice(ODD_VALUES))
        else:
            target.insert(rng.randrange(len(target) + 1), copy.deepcopy(rng.choice(ODD_VALUES + target)))
    else:
        strings = [(c, k) for c, k in positions if isinstance(c[k], str) and c[k]]
        container, key = rng.choice(strings)
        text = container[key]
        position = rng.randrange(len(text))
        container[key] = text[:position] + rng.choice("0123456789abcdefZ -\n.") + text[position + 1:]
    return changed


def _same(a, b) -> bool:
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except (TypeError, ValueError):
        return False


def test_seeded_mutations_of_a_proof_never_raise_and_are_never_accepted():
    rng = random.Random(20261117)
    trees = [build(ABCD), numbered(11), build([("only", 5)])]
    context = SumTreeContext("conversion-liabilities", "example-institution", "FICTIONAL_W", 2,
                             "2099-12-31T15:00:01.000000Z", bytes.fromhex("22" * 32))
    trees.append(build([("a", 1, '{"source_amount":"1","target_slot_id":"t"}'), ("b", 2, "{}"), ("c", 0, "")], context))
    changed_count = accepted = 0
    reasons: dict[str, int] = {}
    while changed_count < 12_000:
        tree = rng.choice(trees)
        slot = rng.choice(tree.leaves).slot_id
        genuine = tree.proof_for(slot)
        candidate = mutate(genuine, rng)
        if _same(candidate, genuine):
            continue
        changed_count += 1
        # the unguarded routine must not raise either
        inner = sumtree_mod._verify_inclusion(candidate, tree.root, slot)
        result = check(candidate, tree.root, slot)
        assert (inner.ok, inner.reason) == (result.ok, result.reason)
        accepted += result.ok
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    assert accepted == 0
    assert None not in reasons
    assert {"MALFORMED_PROOF", "ROOT_MISMATCH", "UNSUPPORTED_FORMAT", "PATH_LENGTH_MISMATCH"} <= set(reasons)


def test_seeded_mutations_of_an_export_never_raise_and_are_never_accepted():
    rng = random.Random(1117)
    trees = [build(ABCD), numbered(9), build([])]
    changed_count = accepted = 0
    reasons: dict[str, int] = {}
    while changed_count < 1_500:
        tree = rng.choice(trees)
        genuine = tree.export()
        candidate = mutate(genuine, rng)
        if _same(candidate, genuine):
            continue
        changed_count += 1
        inner = sumtree_mod._audit_export(candidate, tree.root)
        result = audit_sum_tree_export(candidate, expected_root=tree.root)
        assert (inner.ok, inner.reason) == (result.ok, result.reason)
        accepted += result.ok
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    assert accepted == 0
    assert {"MALFORMED_EXPORT", "ROOT_MISMATCH", "UNSUPPORTED_FORMAT"} <= set(reasons)


def test_deeply_nested_or_huge_documents_do_not_raise(five):
    tree, slot, proof = five
    deep: list = []
    for _ in range(3000):
        deep = [deep]
    nested = _mutated(proof, lambda p: p.__setitem__("path", deep))
    assert check(nested, tree.root, slot).reason == "MALFORMED_PROOF"
    long_path = _mutated(proof, lambda p: p.__setitem__("path", [p["path"][0]] * 5000))
    assert check(long_path, tree.root, slot).reason == "PATH_LENGTH_MISMATCH"
    cyclic = copy.deepcopy(proof)
    cyclic["leaf"]["payload"] = cyclic
    assert check(cyclic, tree.root, slot).reason == "MALFORMED_PROOF"


# ---------------------------------------------------------------------------
# 11. Size
# ---------------------------------------------------------------------------


def test_ten_thousand_leaves_build_and_three_proofs_verify():
    items = [(f"account-{i:05d}", (i * 104_729) % 1_000_003) for i in range(10_000)]
    tree = build(items)
    assert tree.leaf_count == 10_000
    assert tree.total == sum(amount for _, amount in items)
    for slot in (tree.leaves[0].slot_id, tree.leaves[4_999].slot_id, tree.leaves[9_999].slot_id):
        proof = tree.proof_for(slot)
        assert len(proof["path"]) <= 14
        result = check(proof, tree.root, slot)
        assert result.ok and result.details["total"] == tree.total and result.details["leaf_count"] == 10_000
