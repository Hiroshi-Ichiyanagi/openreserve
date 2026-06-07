"""Show determinism: same ledger state + same explicit time -> identical hash.

Also shows that the explicit time is required — generation has no wall-clock fallback.

Run: python examples/reproducibility.py
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
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_ledger() -> Ledger:
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve")
    alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")
    for dst, units in ((reserve, 1_000_000), (alice, 300_000)):
        b = TransactionBuilder("FUND", "ops", initiated_at=T)
        b.transfer(platform.account_id, dst.account_id, Money.from_units(units, Currency.JPY))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T)
    return ledger


def main() -> None:
    ledger = build_ledger()
    gen = ProofOfReservesGenerator(ledger)

    # Same ledger state + same snapshot_at -> identical Merkle root.
    root_a = gen.generate(currency=Currency.JPY, snapshot_at=T)[0].user_liabilities_root
    root_b = gen.generate(currency=Currency.JPY, snapshot_at=T)[0].user_liabilities_root
    print("root (run A):", root_a[:24], "...")
    print("root (run B):", root_b[:24], "...")
    print("identical:", root_a == root_b)

    # The explicit time is required: there is no wall-clock fallback.
    try:
        gen.generate(currency=Currency.JPY)  # type: ignore[call-arg]
    except TypeError as e:
        print("\nsnapshot_at is required (no wall-clock fallback):")
        print("  TypeError:", e)


if __name__ == "__main__":
    main()
