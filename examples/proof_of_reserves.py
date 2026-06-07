"""Generate a proof-of-reserves and let one user verify their own inclusion.

Run: python examples/proof_of_reserves.py
"""
from datetime import datetime, timezone

from openreserve import (
    Currency,
    Ledger,
    Money,
    OwnerType,
    ProofOfReservesGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
    verify_proof_with_node_prefix,
)

# An explicit point in time — proof generation never reads the wall clock.
T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fund(ledger, src, dst, units):
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, Money.from_units(units, Currency.JPY))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


def main() -> None:
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve")
    alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
    bob = ledger.open_account(OwnerType.USER, Currency.JPY, "bob")

    fund(ledger, platform, reserve, 1_000_000)  # reserve assets
    fund(ledger, platform, alice, 300_000)      # user balances (liabilities)
    fund(ledger, platform, bob, 150_000)

    gen = ProofOfReservesGenerator(ledger)
    proof, tree = gen.generate(currency=Currency.JPY, snapshot_at=T)

    summary = proof.to_public_summary()
    print("--- public summary (no individual balances) ---")
    for key in (
        "currency",
        "user_count",
        "user_liabilities_total",
        "reserve_assets_total",
        "is_solvent",
        "coverage_ratio_basis_points",
        "user_liabilities_merkle_root",
    ):
        print(f"  {key}: {summary[key]}")

    # Alice verifies her own balance is included under the published root,
    # without seeing anyone else's balance.
    alice_proof = tree.proof_for(alice.account_id)
    ok = verify_proof_with_node_prefix(alice_proof)
    print("\n--- alice's inclusion proof ---")
    print("  balance_cents:", alice_proof.leaf.balance_cents)
    print("  verifies:", ok)
    print("  root matches published:", alice_proof.root_hash == proof.user_liabilities_root)


if __name__ == "__main__":
    main()
