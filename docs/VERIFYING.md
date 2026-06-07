# Verifying a proof

openreserve's point is that you don't have to trust the operator's dashboard — you can
re-verify a published proof yourself, offline. This guide shows how.

There are two independent things a third party can check:

1. **Solvency arithmetic** — from the public summary alone.
2. **Inclusion** — that a specific account is included under the published Merkle root,
   using that account's Merkle proof.

## What gets published

A `ProofOfReserves.to_public_summary()` is a JSON-serializable dict. It contains the
Merkle root, the liability total, the reserve total, the solvency flag, and the coverage
ratio — but **not** individual user balances. An operator can publish this at a static
URL; anyone can fetch it (e.g. with `curl`) and check it.

For inclusion, an individual account holder is given their own `MerkleProof` (leaf +
sibling steps + root). That proof reveals only their own leaf, not other balances.

## 1. Re-check solvency from the summary

Given a published `summary.json`:

```python
import json

summary = json.load(open("summary.json"))

liabilities = summary["user_liabilities_total"]
reserves = summary["reserve_assets_total"]

assert reserves >= liabilities, "reserves do not cover liabilities"
assert summary["is_solvent"] == (reserves >= liabilities)
print("solvency OK:", reserves, ">=", liabilities)
```

This requires nothing but the published JSON.

## 2. Re-verify an inclusion proof offline

An account holder (or anyone they share the proof with) can confirm their balance is
included under the published root, without the operator's system:

```python
from openreserve import MerkleLeaf, MerkleProof, verify_proof_with_node_prefix
# MerkleProofStep is a structural detail not re-exported at the top level:
from openreserve.transparency.merkle import MerkleProofStep

proof_json = json.load(open("user_proof.json"))

proof = MerkleProof(
    leaf=MerkleLeaf(**proof_json["leaf"]),
    steps=tuple(MerkleProofStep(**s) for s in proof_json["steps"]),
    root_hash=proof_json["root_hash"],
)

assert verify_proof_with_node_prefix(proof), "inclusion proof did not verify"
assert proof.root_hash == summary["user_liabilities_merkle_root"]
print("inclusion OK for", proof.leaf.account_id, "balance", proof.leaf.balance_cents)
```

A runnable, end-to-end version of this (publish to a file, then verify in a separate
step) is in [`examples/publish_and_verify_offline.py`](../examples/publish_and_verify_offline.py).

## 3. Check determinism yourself ("verify me")

The reason a published proof is reproducible at all is that generation never reads the
wall clock. You can confirm the contract:

```bash
python -m pytest tests/test_determinism_guard.py -v
```

It asserts that the same ledger state plus the same `snapshot_at` reproduces the same
Merkle root and audit hash, and that generation still works with `datetime.now` patched
to raise.

## Note on scope

Determinism here is on the **time axis**: a given ledger state at a given time produces
identical artifacts. Identifiers (`account_id`, `transaction_id`) are UUID-based, so an
independently rebuilt ledger will not share them — what a third party verifies is a
*published* proof, not a from-scratch reconstruction. See [ROADMAP.md](../ROADMAP.md).
