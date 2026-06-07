"""Publish a proof as JSON, then verify it offline in a separate step.

The "verify" step reads only the published JSON files — it does not touch the ledger,
demonstrating that a third party can re-check a published proof on their own.

Run: python examples/publish_and_verify_offline.py
"""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from openreserve import (
    Currency,
    Ledger,
    MerkleLeaf,
    MerkleProof,
    Money,
    OwnerType,
    ProofOfReservesGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
    verify_proof_with_node_prefix,
)

# MerkleProofStep is a structural detail, imported from its module (not the top-level API).
from openreserve.transparency.merkle import MerkleProofStep

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fund(ledger, src, dst, units):
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, Money.from_units(units, Currency.JPY))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


def publish(out_dir: Path) -> str:
    """Operator side: build a ledger, generate a proof, write JSON artifacts."""
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve")
    alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
    fund(ledger, platform, reserve, 1_000_000)
    fund(ledger, platform, alice, 300_000)

    proof, tree = ProofOfReservesGenerator(ledger).generate(currency=Currency.JPY, snapshot_at=T)

    (out_dir / "summary.json").write_text(json.dumps(proof.to_public_summary(), indent=2))

    mp = tree.proof_for(alice.account_id)
    user_proof = {
        "leaf": {
            "account_id": mp.leaf.account_id,
            "balance_cents": mp.leaf.balance_cents,
            "currency_code": mp.leaf.currency_code,
        },
        "steps": [{"sibling_hash": s.sibling_hash, "is_left": s.is_left} for s in mp.steps],
        "root_hash": mp.root_hash,
    }
    (out_dir / "alice_proof.json").write_text(json.dumps(user_proof, indent=2))
    return alice.account_id


def verify_offline(out_dir: Path) -> None:
    """Verifier side: read only the published JSON, re-check everything."""
    summary = json.loads((out_dir / "summary.json").read_text())

    # 1) Solvency arithmetic, from the summary alone.
    liabilities = summary["user_liabilities_total"]
    reserves = summary["reserve_assets_total"]
    assert reserves >= liabilities, "reserves do not cover liabilities"
    assert summary["is_solvent"] == (reserves >= liabilities)
    print("solvency OK:", reserves, ">=", liabilities)

    # 2) Inclusion proof, reconstructed from JSON and verified cryptographically.
    pj = json.loads((out_dir / "alice_proof.json").read_text())
    mp = MerkleProof(
        leaf=MerkleLeaf(**pj["leaf"]),
        steps=tuple(MerkleProofStep(**s) for s in pj["steps"]),
        root_hash=pj["root_hash"],
    )
    assert verify_proof_with_node_prefix(mp), "inclusion proof did not verify"
    assert mp.root_hash == summary["user_liabilities_merkle_root"], "root mismatch"
    print("inclusion OK:", mp.leaf.account_id, "balance", mp.leaf.balance_cents)


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        publish(out)
        print("published:", sorted(p.name for p in out.iterdir()))
        verify_offline(out)


if __name__ == "__main__":
    main()
