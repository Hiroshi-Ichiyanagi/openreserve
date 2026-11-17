"""tests/test_superrich_guards.py — portfolio / audit as-of / import 規律 / 決定論ガード。"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.core.ledger as ledger_mod
import openreserve.core.storage as storage_mod
import openreserve.transparency.audit_log as audit_mod
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Currency, Money, OwnerType
from openreserve.execution import SolverBid, SurplusSplitPolicy, ExecutionAccounts, run_auction
from openreserve.intent import (
    IntentSettlementEngine,
    MandatePortfolioProofGenerator,
    ProofOfMandateGenerator,
    audit_root_as_of,
)
from openreserve.transparency.audit_log import GENESIS_HASH, AuditLog

REPO = Path(__file__).parent.parent
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = T + timedelta(days=30)


def usdc(n: int) -> Money:
    return Money.from_units(n, Currency.USDC)


def _fund(ledger, src, dst, money):
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, money)
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


# ---------- Portfolio ----------


class TestPortfolio:
    def _two_mandates(self, cap1=100, cap2=100, shared_escrow=False):
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        audit = AuditLog(":memory:")
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e1 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e1")
        _fund(ledger, plat, e1, usdc(1000))
        e2 = e1 if shared_escrow else ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e2")
        if not shared_escrow:
            _fund(ledger, plat, e2, usdc(1000))
        pa = ledger.open_account(OwnerType.USER, Currency.USDC, "pa")
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e1.account_id, budget_cap=usdc(cap1), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        eng.create_mandate(mandate_id="m2", principal_id="p", escrow_account_id=e2.account_id, budget_cap=usdc(cap2), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        return ledger, eng, e1, e2, pa

    def test_all_within_budget(self):
        ledger, eng, e1, e2, pa = self._two_mandates()
        eng.settle_intent(mandate_id="m1", intent_id="i", payee_account_id=pa.account_id, amount=usdc(30), purpose="A", at=T)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        assert proof.all_within_budget
        assert proof.mandate_count == 2

    def test_shared_escrow_counted_once(self):
        ledger, eng, e1, e2, pa = self._two_mandates(shared_escrow=True)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        # shared escrow (1000) counted once, not 2000
        assert proof.total_escrow_balance_cents == usdc(1000).cents

    def test_under_collateralized_flagged_no_raise(self):
        # 共有 escrow を薄くして remaining 合計 > escrow にする
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e")
        _fund(ledger, plat, e, usdc(100))
        eng = IntentSettlementEngine(ledger, audit_log=AuditLog(":memory:"))
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        # drain escrow below remaining
        plat2 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat2")
        d = TransactionBuilder("WITHDRAW", "ops", initiated_at=T)
        d.transfer(e.account_id, plat2.account_id, usdc(60))
        ledger.post(d.build())
        ledger.settle(d.transaction_id, settled_at=T)
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1"], snapshot_at=T, currency=Currency.USDC)
        assert proof.is_collateralized is False  # escrow 40 < remaining 100
        assert proof.all_within_budget is True  # budget itself not exceeded

    def test_independent_escrows_not_netted(self):
        # M1 escrow drained to 0 (remaining 100); M2 overfunded to 200 (remaining 100).
        # A global net (200 >= 200) would falsely say collateralized; per-escrow must not.
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        e1 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e1")
        e2 = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "e2")
        _fund(ledger, plat, e1, usdc(100))
        _fund(ledger, plat, e2, usdc(100))
        eng = IntentSettlementEngine(ledger, audit_log=AuditLog(":memory:"))
        eng.create_mandate(mandate_id="m1", principal_id="p", escrow_account_id=e1.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        eng.create_mandate(mandate_id="m2", principal_id="p", escrow_account_id=e2.account_id, budget_cap=usdc(100), allowed_purposes=frozenset(["A"]), valid_from=T, valid_until=END, created_at=T)
        # drain e1 -> 0, overfund e2 -> 200
        d = TransactionBuilder("WITHDRAW", "ops", initiated_at=T)
        d.transfer(e1.account_id, plat.account_id, usdc(100))
        ledger.post(d.build())
        ledger.settle(d.transaction_id, settled_at=T)
        _fund(ledger, plat, e2, usdc(100))
        proof, _ = MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.USDC)
        assert proof.total_escrow_balance_cents == usdc(200).cents
        assert proof.total_remaining_cents == usdc(200).cents
        assert proof.is_collateralized is False  # e1 (0) cannot back m1 (100), despite the global net

    def test_currency_mismatch_rejected(self):
        ledger, eng, e1, e2, pa = self._two_mandates()
        with pytest.raises(Exception):
            MandatePortfolioProofGenerator(eng).generate(["m1", "m2"], snapshot_at=T, currency=Currency.JPY)


# ---------- audit_root_as_of ----------


class TestAuditAsOf:
    def test_excludes_events_after_snapshot(self):
        audit = AuditLog(":memory:")
        audit.append("e1", {"x": 1}, timestamp=T)
        h1 = audit.latest_hash()
        audit.append("e2", {"x": 2}, timestamp=T + timedelta(hours=2))
        # as-of T excludes e2
        assert audit_root_as_of(audit, T) == h1
        assert audit_root_as_of(audit, T + timedelta(hours=3)) == audit.latest_hash()

    def test_pure_function(self):
        audit = AuditLog(":memory:")
        audit.append("e1", {"x": 1}, timestamp=T)
        assert audit_root_as_of(audit, T) == audit_root_as_of(audit, T)

    def test_none_is_genesis(self):
        assert audit_root_as_of(None, T) == GENESIS_HASH


# ---------- Import discipline (execution must not import intent) ----------


class TestImportDiscipline:
    def test_execution_does_not_import_intent(self):
        exec_dir = REPO / "openreserve" / "execution"
        offenders = []
        for py in exec_dir.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("openreserve.intent"):
                    offenders.append((py.name, node.module))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("openreserve.intent"):
                            offenders.append((py.name, alias.name))
        assert offenders == [], f"execution imports intent: {offenders}"


# ---------- No wall clock / no total_seconds in new deterministic modules ----------


def _called_attrs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


NEW_MODULES = [
    "openreserve/execution/routing.py",
    "openreserve/execution/auction.py",
    "openreserve/execution/surplus.py",
    "openreserve/execution/arbitrage.py",
    "openreserve/execution/proofs.py",
    "openreserve/intent/policy.py",
    "openreserve/intent/settlement.py",
    "openreserve/intent/lifecycle.py",
    "openreserve/intent/submandate.py",
    "openreserve/intent/mandate.py",
    "openreserve/intent/bridge.py",
    "openreserve/intent/proofs.py",
    "openreserve/intent/audit_asof.py",
    "openreserve/transparency/audit_query.py",
]

# Modules of the exact-arithmetic and proof core: no clock, no random source, no network, no float.
EXACT_CORE_MODULES = [
    "openreserve/core/exact.py",
    "openreserve/core/currencies.py",
    "openreserve/transparency/sumtree.py",
    "openreserve/transparency/snapshot.py",
    "openreserve/conversion.py",
    "openreserve/conversion_evidence.py",
    "openreserve/transparency/coverage.py",
]

# The store and its migration: no clock, no random source, no network, no float. They use
# sqlite3 and os, so the standard-library allow-list of the exact core does not apply.
STORAGE_MODULES = [
    "openreserve/core/storage.py",
    "openreserve/core/migration.py",
]

FORBIDDEN_CALLS = {"now", "total_seconds", "today", "utcnow", "urandom", "uuid4", "token_bytes", "float"}
FORBIDDEN_IMPORTS = {"random", "secrets", "time", "uuid", "socket", "ssl", "http", "urllib"}


def _called_names(path: Path) -> set[str]:
    """Names of everything that is called: ``x.name(...)`` and ``name(...)``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def _imported_modules(path: Path) -> set[str]:
    """Top-level names of every imported module."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


class TestNoWallClockStatic:
    def test_no_now_or_total_seconds_calls(self):
        for rel in NEW_MODULES + EXACT_CORE_MODULES:
            called = _called_attrs(REPO / rel)
            assert "now" not in called, f"{rel} calls .now() (wall-clock leak)"
            assert "total_seconds" not in called, f"{rel} calls .total_seconds() (float)"


class TestExactCoreStatic:
    """The exact-arithmetic and proof core reads no clock, no random source and no network,
    and never builds a float."""

    def test_modules_exist(self):
        for rel in EXACT_CORE_MODULES:
            assert (REPO / rel).is_file(), rel

    def test_no_forbidden_calls(self):
        for rel in EXACT_CORE_MODULES:
            offending = _called_names(REPO / rel) & FORBIDDEN_CALLS
            assert not offending, f"{rel} calls {sorted(offending)}"

    def test_no_forbidden_imports(self):
        for rel in EXACT_CORE_MODULES:
            offending = _imported_modules(REPO / rel) & FORBIDDEN_IMPORTS
            assert not offending, f"{rel} imports {sorted(offending)}"

    def test_only_the_standard_library_and_the_package_are_imported(self):
        allowed = {"__future__", "dataclasses", "datetime", "fractions", "hashlib", "hmac", "json", "re", "typing", "openreserve"}
        for rel in EXACT_CORE_MODULES:
            extra = _imported_modules(REPO / rel) - allowed
            assert not extra, f"{rel} imports {sorted(extra)}"

    def test_no_float_literals(self):
        for rel in EXACT_CORE_MODULES:
            tree = ast.parse((REPO / rel).read_text())
            floats = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
            assert not floats, f"{rel} contains float literals {floats}"

    def test_storage_and_migration_read_no_clock_and_build_no_float(self):
        for rel in STORAGE_MODULES:
            assert (REPO / rel).is_file(), rel
            offending = _called_names(REPO / rel) & FORBIDDEN_CALLS
            assert not offending, f"{rel} calls {sorted(offending)}"
            offending = _imported_modules(REPO / rel) & FORBIDDEN_IMPORTS
            assert not offending, f"{rel} imports {sorted(offending)}"
            tree = ast.parse((REPO / rel).read_text())
            floats = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
            assert not floats, f"{rel} contains float literals {floats}"

    def test_legacy_reserve_artifact_builds_no_float(self):
        rel = "openreserve/transparency/proofs.py"
        assert "float" not in _called_names(REPO / rel), f"{rel} calls float()"
        tree = ast.parse((REPO / rel).read_text())
        floats = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
        assert not floats, f"{rel} contains float literals {floats}"

    def test_the_guard_helpers_see_what_they_should(self, tmp_path):
        sample = tmp_path / "sample.py"
        sample.write_text(
            "import time\nimport urllib.request\nfrom secrets import token_bytes\nfrom . import sibling\n"
            "x = float('1')\ny = datetime.now()\nz = os.urandom(8)\n"
        )
        assert _called_names(sample) >= {"float", "now", "urandom"}
        assert _imported_modules(sample) == {"time", "urllib", "secrets"}


# ---------- Determinism + no-wall-clock runtime guard ----------


class _NowForbidden:
    @staticmethod
    def now(*a, **k):
        raise AssertionError("datetime.now() called on a deterministic path (wall-clock leak)")

    fromisoformat = staticmethod(datetime.fromisoformat)
    max = datetime.max
    min = datetime.min


class TestDeterminismRuntime:
    def _run_auction_flow(self):
        ledger = Ledger(SQLiteLedgerStorage(":memory:"))
        audit = AuditLog(":memory:")
        plat = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "plat")
        escrow = ledger.open_account(OwnerType.PLATFORM, Currency.USDC, "escrow")
        _fund(ledger, plat, escrow, usdc(1000))
        payee = ledger.open_account(OwnerType.USER, Currency.USDC, "payee")
        rebate = ledger.open_account(OwnerType.USER, Currency.USDC, "rebate")
        margin = ledger.open_account(OwnerType.FEE, Currency.USDC, "margin")
        gas = ledger.open_account(OwnerType.FEE, Currency.USDC, "gas")
        eng = IntentSettlementEngine(ledger, audit_log=audit)
        eng.create_mandate(mandate_id="m", principal_id="agent", escrow_account_id=escrow.account_id, budget_cap=usdc(1000), allowed_purposes=frozenset(["SWAP"]), valid_from=T, valid_until=END, created_at=T)
        accts = ExecutionAccounts(payee_account_id=payee.account_id, user_rebate_account_id=rebate.account_id, operator_margin_account_id=margin.account_id, gas_account_id=gas.account_id)
        auc = run_auction([SolverBid("s1", "i1", usdc(90).cents, 0, 0)], objective="MIN_COST")
        eng.settle_via_auction(mandate_id="m", intent_id="i1", user_limit=usdc(100), purpose="SWAP", at=T, auction=auc, split_policy=SurplusSplitPolicy(user_rebate_bps=7000, operator_margin_bps=3000), accounts=accts)
        return eng

    def test_reproducible_proof(self):
        eng = self._run_auction_flow()
        p1, t1 = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
        p2, t2 = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
        assert p1.to_public_summary() == p2.to_public_summary()
        assert t1.root_hash == t2.root_hash

    def test_no_wallclock_across_auction_and_proofs(self):
        orig = (ledger_mod.datetime, storage_mod.datetime, audit_mod.datetime)
        ledger_mod.datetime = _NowForbidden  # type: ignore[assignment]
        storage_mod.datetime = _NowForbidden  # type: ignore[assignment]
        audit_mod.datetime = _NowForbidden  # type: ignore[assignment]
        try:
            eng = self._run_auction_flow()
            proof, _ = ProofOfMandateGenerator(eng).generate("m", snapshot_at=T)
            assert proof.is_within_budget
            ok, _ = eng.audit_log.verify_chain()
            assert ok
        finally:
            ledger_mod.datetime, storage_mod.datetime, audit_mod.datetime = orig
