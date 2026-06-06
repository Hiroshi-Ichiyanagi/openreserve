"""openreserve — deterministic, offline-verifiable proof-of-reserves + audit chain.

Public API re-exports. See README.md for usage.
"""

from openreserve.core import (
    Account,
    ComplianceDecision,
    Currency,
    Entry,
    Ledger,
    LedgerStorage,
    Money,
    OwnerType,
    SQLiteLedgerStorage,
    Transaction,
    TransactionBuilder,
    TransactionStatus,
)
from openreserve.providers import ProviderCategory
from openreserve.regulatory import (
    FundRetentionAnalysis,
    PaymentServicesActCompliance,
    ReserveDepositCalculation,
    TransactionLimitViolation,
)
from openreserve.transparency import (
    AuditEvent,
    AuditLog,
    ComplianceReport,
    GENESIS_HASH,
    MerkleLeaf,
    MerkleProof,
    MerkleTree,
    ProofOfComplianceGenerator,
    ProofOfReserves,
    ProofOfReservesGenerator,
    ProofOfSolvency,
    ProofOfSolvencyGenerator,
    verify_proof_with_node_prefix,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # core ledger
    "Account",
    "ComplianceDecision",
    "Currency",
    "Entry",
    "Ledger",
    "LedgerStorage",
    "Money",
    "OwnerType",
    "SQLiteLedgerStorage",
    "Transaction",
    "TransactionBuilder",
    "TransactionStatus",
    # transparency: merkle / audit chain / proofs
    "AuditEvent",
    "AuditLog",
    "ComplianceReport",
    "GENESIS_HASH",
    "MerkleLeaf",
    "MerkleProof",
    "MerkleTree",
    "ProofOfComplianceGenerator",
    "ProofOfReserves",
    "ProofOfReservesGenerator",
    "ProofOfSolvency",
    "ProofOfSolvencyGenerator",
    "verify_proof_with_node_prefix",
    # regulatory: reserve / deposit calculation
    "FundRetentionAnalysis",
    "PaymentServicesActCompliance",
    "ReserveDepositCalculation",
    "TransactionLimitViolation",
    # providers
    "ProviderCategory",
]
