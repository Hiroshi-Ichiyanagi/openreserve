# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
APIs may change while the project is pre-1.0.

## [Unreleased]

_No unreleased changes yet._

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

[Unreleased]: https://github.com/Hiroshi-Ichiyanagi/openreserve/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Hiroshi-Ichiyanagi/openreserve/releases/tag/v0.1.0
