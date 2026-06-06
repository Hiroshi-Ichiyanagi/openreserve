"""
tests/test_determinism_guard.py — determinism guard for the verification core.

Contract:
    Proof artifact generation (proof-of-reserves, audit-chain) takes an explicit
    as-of / event time and MUST NOT fall back to the wall clock (datetime.now()).

This guard lets a user verify the contract themselves ("verify me"):
    (a) reproducibility   : identical inputs + identical explicit time -> identical hash
    (b) no-wall-clock     : with the artifact module's datetime.now patched to raise,
                            generation with an explicit time still succeeds (never
                            touches the wall clock)

Scope note:
    transaction_id / account_id are uuid4-based and therefore vary across runs
    (an identifier-determinism axis tracked separately). This guard covers the
    *time* axis: artifacts are reproducible given a fixed ledger state + fixed time.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.transparency.audit_log as audit_mod
import openreserve.transparency.proofs as proofs_mod
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Currency, Money, OwnerType
from openreserve.transparency.audit_log import AuditLog
from openreserve.transparency.proofs import ProofOfReservesGenerator


T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
SNAP = datetime(2026, 6, 5, tzinfo=timezone.utc)


class _NowForbidden:
    """datetime stand-in: now() is forbidden (raises); fromisoformat delegates.

    Used to prove that artifact generation never reads the wall clock.
    """

    @staticmethod
    def now(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "datetime.now() called during deterministic artifact generation "
            "(wall-clock leak)"
        )

    fromisoformat = staticmethod(datetime.fromisoformat)


def _build_reserves_ledger() -> Ledger:
    """Deterministic ledger with reserves > liabilities (all tx settled at T0)."""
    ledger = Ledger(SQLiteLedgerStorage(":memory:"))
    platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
    reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve_jpy")

    def _settle(src, dst, units):
        b = TransactionBuilder("FUND", "test", initiated_at=T0)
        b.transfer(src.account_id, dst.account_id, Money.from_units(units, Currency.JPY))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=T0)

    _settle(platform, reserve, 1_000_000)
    for i, amt in enumerate((100_000, 200_000, 50_000)):
        user = ledger.open_account(OwnerType.USER, Currency.JPY, f"user_{i}")
        _settle(platform, user, amt)
    return ledger


class TestAuditChainDeterminism:
    """The audit-chain hash is wall-clock independent and deterministic."""

    def test_hash_is_deterministic_given_identical_inputs(self) -> None:
        """Identical (event_type, payload, timestamp) sequence -> identical latest_hash.

        compute_hash includes the timestamp, so passing an explicit timestamp makes
        the chain reproducible regardless of the wall clock.
        """
        events = [
            ("account_opened", {"account_id": "acct_fixed_1", "label": "alice"}),
            ("transaction_posted", {"transaction_id": "tx_fixed_1", "status": "SETTLED"}),
            ("transaction_settled", {"transaction_id": "tx_fixed_1", "settled_at": T0.isoformat()}),
        ]
        stamps = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]

        log_a = AuditLog(":memory:")
        log_b = AuditLog(":memory:")
        for (etype, payload), ts in zip(events, stamps):
            log_a.append(etype, payload, timestamp=ts)
            log_b.append(etype, payload, timestamp=ts)

        assert log_a.latest_hash() == log_b.latest_hash()
        assert log_a.latest_hash() != audit_mod.GENESIS_HASH
        ok_a, _ = log_a.verify_chain()
        ok_b, _ = log_b.verify_chain()
        assert ok_a and ok_b
        log_a.close()
        log_b.close()

    def test_append_never_calls_wallclock_when_timestamp_given(self) -> None:
        """With audit datetime.now forbidden, explicit-timestamp append still works."""
        original = audit_mod.datetime
        audit_mod.datetime = _NowForbidden  # type: ignore[assignment]
        try:
            log = AuditLog(":memory:")
            log.append("e1", {"x": 1}, timestamp=T0)
            log.append("e2", {"x": 2}, timestamp=T0 + timedelta(hours=1))
            ok, err = log.verify_chain()
            assert ok, f"chain invalid: {err}"
            log.close()
        finally:
            audit_mod.datetime = original  # type: ignore[assignment]


class TestProofOfReservesDeterminism:
    """proof-of-reserves is deterministic and wall-clock independent."""

    def test_same_ledger_same_snapshot_is_reproducible(self) -> None:
        """Same ledger + same snapshot_at -> identical public summary and Merkle root."""
        ledger = _build_reserves_ledger()
        gen = ProofOfReservesGenerator(ledger)

        proof1, tree1 = gen.generate(currency=Currency.JPY, snapshot_at=SNAP)
        proof2, tree2 = gen.generate(currency=Currency.JPY, snapshot_at=SNAP)

        assert proof1.to_public_summary() == proof2.to_public_summary()
        assert proof1.user_liabilities_root == proof2.user_liabilities_root
        assert tree1.root_hash == tree2.root_hash
        assert proof1.is_solvent
        assert proof1.user_liabilities_total_cents == 350_000

    def test_snapshot_at_is_required_no_wallclock_fallback(self) -> None:
        """Omitting snapshot_at raises TypeError (no wall-clock fallback)."""
        ledger = _build_reserves_ledger()
        gen = ProofOfReservesGenerator(ledger)
        with pytest.raises(TypeError):
            gen.generate(currency=Currency.JPY)  # type: ignore[call-arg]

    def test_generation_never_calls_wallclock(self) -> None:
        """With proofs datetime.now forbidden, explicit-snapshot generation succeeds."""
        ledger = _build_reserves_ledger()
        gen = ProofOfReservesGenerator(ledger)
        original = proofs_mod.datetime
        proofs_mod.datetime = _NowForbidden  # type: ignore[assignment]
        try:
            proof, _tree = gen.generate(currency=Currency.JPY, snapshot_at=SNAP)
            assert proof.snapshot_at == SNAP
        finally:
            proofs_mod.datetime = original  # type: ignore[assignment]
