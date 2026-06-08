# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
APIs may change while the project is pre-1.0.

## [Unreleased]

### Added
- Documentation: README "Use cases", "Proof of Reserves vs Proof of Solvency",
  a verification-flow diagram, and a "Limitations" section; the same verification-flow
  diagram in `docs/VERIFYING.md` and a PoR-vs-PoS note in `docs/ARCHITECTURE.md`.

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
