"""Public names and the deprecation period of the legacy names (design section 9.3).

New public names do not say "solvency" or "solvent": the check compares two booked totals.
The legacy names keep working with their old signatures and artifacts; calling the legacy
generators or ``verify_proof_with_node_prefix`` emits a ``PendingDeprecationWarning`` that
names the replacement. The README example keeps running the legacy path unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve
import openreserve.conversion as conversion_mod
import openreserve.conversion_evidence as evidence_mod
from openreserve import (
    Currency,
    DeclaredReserveCoverageGenerator,
    Ledger,
    MerkleLeaf,
    MerkleTree,
    Money,
    OwnerType,
    ProofOfReservesGenerator,
    ProofOfSolvencyGenerator,
    ReserveCoverageGenerator,
    SQLiteLedgerStorage,
    TransactionBuilder,
    verify_proof_with_node_prefix,
)
from openreserve.transparency.audit_log import GENESIS_HASH

REPO = Path(__file__).parent.parent
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = bytes(range(32))

# Every public name added by the changes of the design document, by the module that defines it.
NEW_NAMES = {
    "openreserve.core": [
        "AMOUNT_MAX", "INSTANT_MAX", "CurrencySpec", "CurrencySpecError", "ExactValueError", "MigrationError",
        "NaiveDatetimeError", "SCHEMA_VERSION", "SchemaVersionError", "amount_from_text", "amount_to_text",
        "amount_to_u128", "canonical_json", "instant_from_stored_text", "instant_from_text", "instant_to_text",
        "migrate_ledger_database", "require_aware", "sha256_hex",
        "CurrencyRegistry", "CurrencyRegistryError", "DEFAULT_REGISTRY",
    ],
    "openreserve.transparency": [
        "CoverageFigures", "DeclaredReserveCoverage", "DeclaredReserveCoverageGenerator", "LiabilitiesSnapshot",
        "LiabilitiesSnapshotGenerator", "ReserveCoverage", "ReserveCoverageGenerator", "SumTree", "SumTreeContext",
        "SumTreeError", "SumTreeLeaf", "Verification", "audit_snapshot_export", "audit_sum_tree_export",
        "collect_user_balances", "coverage_figures", "derive_salt", "verify_reserve_coverage_summary",
        "verify_snapshot_inclusion", "verify_sum_tree_inclusion",
    ],
    "openreserve.conversion": list(conversion_mod.__all__),
    "openreserve.conversion_evidence": list(evidence_mod.__all__),
}

LEGACY_NAMES = [
    "ProofOfReserves", "ProofOfReservesGenerator", "ProofOfSolvency", "ProofOfSolvencyGenerator",
    "verify_proof_with_node_prefix", "MerkleTree", "MerkleProof", "MerkleLeaf",
]


def _ledger() -> Ledger:
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


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", sorted(NEW_NAMES))
def test_every_new_name_is_importable_from_the_top_level(module_name):
    module = __import__(module_name, fromlist=["_"])
    for name in NEW_NAMES[module_name]:
        assert name in openreserve.__all__, name
        assert getattr(openreserve, name) is getattr(module, name), name


def test_top_level_all_resolves_and_has_no_duplicates():
    assert len(openreserve.__all__) == len(set(openreserve.__all__))
    for name in openreserve.__all__:
        assert hasattr(openreserve, name), name


def test_new_names_do_not_say_solvency():
    for names in NEW_NAMES.values():
        for name in names:
            assert "solven" not in name.lower(), name


def test_legacy_names_still_import():
    for name in LEGACY_NAMES:
        assert name in openreserve.__all__, name
        assert getattr(openreserve, name) is not None


# ---------------------------------------------------------------------------
# The deprecation period
# ---------------------------------------------------------------------------


def _only_warning(record) -> warnings.WarningMessage:
    pending = [w for w in record if issubclass(w.category, PendingDeprecationWarning)]
    assert len(pending) == 1, [str(w.message) for w in pending]
    assert not [w for w in record if w.category is DeprecationWarning]
    return pending[0]


def test_legacy_reserve_generator_runs_and_warns_with_the_replacement():
    ledger = _ledger()
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        proof, tree = ProofOfReservesGenerator(ledger).generate(currency=Currency.JPY, snapshot_at=T)
    warning = _only_warning(record)
    assert "ReserveCoverageGenerator" in str(warning.message)
    assert warning.filename == __file__  # stacklevel points at the caller
    assert (proof.is_solvent, proof.user_liabilities_total_cents, proof.reserve_assets_total_cents) == (True, 300_000, 1_000_000)
    assert tree.root_hash == proof.user_liabilities_root


def test_legacy_aggregate_runs_and_warns_once_for_several_currencies():
    ledger = _ledger()
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        proof, trees = ProofOfSolvencyGenerator(ledger).generate((Currency.JPY, Currency.USD), GENESIS_HASH, T)
    warning = _only_warning(record)
    assert "DeclaredReserveCoverageGenerator" in str(warning.message)
    assert warning.filename == __file__
    assert proof.overall_solvent is True and set(trees) == {Currency.JPY, Currency.USD}


def test_verify_proof_with_node_prefix_runs_and_warns_with_the_replacement():
    tree = MerkleTree([MerkleLeaf("alice", 300_000, "JPY"), MerkleLeaf("bob", 120_000, "JPY")])
    proof = tree.proof_for("alice")
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        ok = verify_proof_with_node_prefix(proof)
    warning = _only_warning(record)
    assert "proof.verify(expected_root=" in str(warning.message)
    assert warning.filename == __file__
    assert ok is True


def test_the_replacements_do_not_warn():
    ledger = _ledger()
    tree = MerkleTree([MerkleLeaf("alice", 300_000, "JPY"), MerkleLeaf("bob", 120_000, "JPY")])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert tree.proof_for("alice").verify(expected_root=tree.root_hash)
        coverage = ReserveCoverageGenerator(ledger, institution_id="example-institution", salt_key=KEY).generate(Currency.JPY, T)
        declared = DeclaredReserveCoverageGenerator(ledger, institution_id="example-institution", salt_key=KEY).generate(
            (Currency.JPY,), GENESIS_HASH, T
        )
    assert coverage.is_covered and declared.all_covered


def test_new_and_legacy_agree_on_the_totals_and_the_verdict():
    ledger = _ledger()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PendingDeprecationWarning)
        legacy, _ = ProofOfReservesGenerator(ledger).generate(currency=Currency.JPY, snapshot_at=T)
    coverage = ReserveCoverageGenerator(ledger, institution_id="example-institution", salt_key=KEY).generate(Currency.JPY, T)
    assert coverage.liabilities_total == legacy.user_liabilities_total_cents
    assert coverage.reserves_total == legacy.reserve_assets_total_cents
    assert coverage.is_covered == legacy.is_solvent
    assert coverage.coverage_ratio == legacy.coverage_ratio
    assert coverage.coverage_basis_points_floor == legacy.to_public_summary()["coverage_ratio_basis_points"]


# ---------------------------------------------------------------------------
# The README example runs unchanged
# ---------------------------------------------------------------------------


def _readme_usage_example() -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    usage = text.split("\n## Usage\n", 1)[1]
    match = re.search(r"```python\n(.*?)\n```", usage, re.S)
    assert match is not None, "the README has a Python usage example"
    return match.group(1)


def test_readme_example_runs_unchanged_and_prints_no_warning():
    code = _readme_usage_example()
    assert "ProofOfReservesGenerator" in code  # it still shows the legacy path
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PYTHONWARNINGS", None)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""  # a pending deprecation is not shown under the default filters
    lines = [line.split() for line in result.stdout.splitlines()]
    assert lines[0] == ["solvent:", "True"]
    assert lines[1] == ["liabilities:", "300000"]
    assert lines[2] == ["reserves:", "1000000"]
    assert lines[4] == ["reproducible:", "True"]
