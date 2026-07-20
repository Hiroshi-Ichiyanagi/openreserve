# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
APIs may change while the project is pre-1.0.

## [Unreleased]

### Added
- **`openreserve.intent`** — intent/mandate-based autonomous settlement: `Mandate` +
  `IntentSettlementEngine` (budget-bounded M2M settlement re-derived structurally from the
  ledger, every accept/deny on the audit chain), and `ProofOfMandate` /
  `MandatePortfolioProof` (deterministic, offline-verifiable). Rich policy (`SpendPolicy`,
  deterministic `EpochSpec`, velocity/epoch/window caps, per-purpose sub-budgets, payee
  allowlist), session-key **sub-mandates** with provable ancestor non-leakage, atomic
  **batch** settlement, **lifecycle** (pause/resume/revoke/top-up/expiry) re-derived from
  the audit chain, and nonce/replay + monotonic-time protection. Canonical `DENIAL_ORDER`.
- **`openreserve.execution`** — best-execution + MEV/arbitrage recapture (standalone;
  imports nothing from `intent`): `select_best_route`, `run_auction`, integer-exact
  `compute_split` (`SurplusSplitPolicy`), memo-only `find_best_arbitrage_cycle`, and
  `ProofOfBestExecution` / `ProofOfSurplusRecapture`.
- **Bridge** `settle_via_auction` / `settle_batch_via_auction` (`intent.bridge`) — settle a
  mandate intent through a solver auction and recapture the surplus in one balanced
  transaction, preserving every budget invariant (INV-9: budget-side proof fields are
  byte-identical to a plain settlement). Cross-currency (Model B) supported via per-currency
  venue-clearing accounts.
- `transparency.audit_query.audit_root_as_of` — deterministic as-of audit commitment shared
  by intent and execution proofs.
- Examples (`intent_mandate_settlement`, `best_execution_recapture`, `session_key_submandate`)
  and docs (`docs/INTENT_MANDATES.md`, `docs/EXECUTION_RECAPTURE.md`).
- Documentation: README "Use cases", "Proof of Reserves vs Proof of Solvency",
  a verification-flow diagram, and a "Limitations" section; the same verification-flow
  diagram in `docs/VERIFYING.md` and a PoR-vs-PoS note in `docs/ARCHITECTURE.md`.

### Notes
- No new runtime dependencies (standard library only). The new modules keep the project's
  determinism discipline (explicit time only; never `datetime.now()` on a proof/settlement
  path), verified by static (AST) and runtime guards.

## [0.1.1] - 2026-06-07

First release published to PyPI. No runtime behavior changes from 0.1.0.

### Added
- PyPI packaging metadata (expanded classifiers, keywords, project URLs).
- Community health files (CONTRIBUTING, CODE_OF_CONDUCT, SECURITY), issue/PR templates,
  editorconfig, and Dependabot for GitHub Actions.
- Documentation (ARCHITECTURE, verification guide) and runnable examples.
- ROADMAP.

### Changed
- Version bump to 0.1.1.

## [0.1.0] - 2026-06-07

Initial public release. Early and unproven; no production adoption.

### Added
- Append-only double-entry ledger with SQLite storage (`openreserve.core`).
- Merkle tree, hash-linked tamper-evident audit log, and proof generators
  (proof-of-reserves, proof-of-solvency, proof-of-compliance) in
  `openreserve.transparency`.
- Point-in-time reserve / deposit calculation (`openreserve.regulatory`).
- Abstract provider interface (`ProviderCategory`) with no bundled implementations.
- Determinism contract: proof generation takes an explicit as-of / event time and never
  reads the wall clock, plus a determinism guard test suite.
- Packaging (PEP 621, standard-library-only runtime) and CI (Python 3.11–3.13 + build
  check).

[Unreleased]: https://github.com/Hiroshi-Ichiyanagi/openreserve/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Hiroshi-Ichiyanagi/openreserve/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Hiroshi-Ichiyanagi/openreserve/releases/tag/v0.1.0
