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
- **Exact encodings** (`openreserve.core.exact`): one bound for every amount
  (`AMOUNT_MAX = 10**38 - 1`), canonical decimal text and a 16-byte form for amounts, a strict
  canonical JSON profile without numbers for every hashed document, and one canonical UTC text
  for instants.
- **Merkle-sum tree** (`transparency.sumtree`, format `openreserve.sumtree/1`): a node hash
  binds both child hashes and both child sums, the root binds the tree context, the leaf count
  and the total; no padding and no duplication; salted leaves ordered by leaf hash; an
  inclusion verifier that takes the expected root and returns a reason code; an auditor export
  checked by full recomputation.
- **Liabilities snapshot** (`transparency.snapshot`, `LiabilitiesSnapshotGenerator`): positive
  customer balances in a liabilities tree, negative ones in a separate receivables tree, never
  netted; exactly the entries settled at or before the instant. The holder check
  `verify_snapshot_inclusion` takes the tree the holder expects (`expected_kind`), so that a
  customer who is owed money is told when their proof is in the receivables tree.
- **Declared reserve coverage** (`transparency.coverage`): `ReserveCoverage` and
  `DeclaredReserveCoverage` (formats `openreserve.reserve-coverage/1` and
  `openreserve.declared-reserve-coverage/1`). The reserve figure is named
  `reserves_self_booked`: the operator's own reserve account balances, not evidence that
  reserve assets exist. Coverage is exact (`covered`, `basis_points_floor`, a `Fraction`).
  `verify_reserve_coverage_summary` checks that the verdicts of a published summary follow
  from the totals it states.
- **Declared conversions** (`openreserve.conversion`, `openreserve.conversion_evidence`): a
  hash-identified declaration (rate as decimal text turned into an exact fraction, declared
  quotation direction, declared tie rule, no rate ever inverted), exact conversion arithmetic,
  posting through one clearing account per currency with replay refusal, closing with the
  whole-ledger rounding residue booked to an institution account, and evidence with three
  checks (manifest, holder bundle with the side the holder expects, auditor export).
- **Currency registry** (`CurrencySpec`, `CurrencyRegistry`, `DEFAULT_REGISTRY`): currencies
  as plain data, an immutable hash-identified list; fictional codes start with `FICTIONAL_`.
  The ledger keeps its closed `Currency` enumeration.
- **Ledger file schema version 1** and `migrate_ledger_database` (see Changed).
- `Ledger.reverse(..., reversed_at=...)` and `Ledger.account_entries(...)`.
- Test vectors in `tests/vectors/` with a standalone recomputation script, and the design
  document `docs/hypothetical/W_CHANGEOVER_DESIGN.md`. It works through a hypothetical
  scenario (a fixed-rate changeover to a fictional unit "W"); it is a design exercise, not a
  forecast or a proposal, and nothing in this project issues or offers any currency or token.

### Changed
- **Breaking:** the SQLite store writes schema version 1 (`PRAGMA user_version`): amounts as
  canonical decimal text, timestamps as canonical UTC text. A ledger file written by 0.1.x is
  refused with `SchemaVersionError` until it is converted once with
  `migrate_ledger_database(path, backup_path=..., applied_at=...)`, which writes a backup
  first. 0.1.x cannot read a converted file.
- **Breaking:** datetimes entering the ledger, the store or a generator must be
  timezone-aware; a naive one raises `NaiveDatetimeError`.
- **Breaking:** `TransactionBuilder.fx_convert` takes one clearing account per currency
  (`from_clearing_account_id`, `to_clearing_account_id`); the old call fails with a `TypeError`.
- **Breaking:** `hash_pair()` is removed from `transparency.merkle`.
- **Breaking:** `ProofOfReserves.coverage_ratio` is a `Fraction`, or `None` when there are no
  liabilities; in its summary `coverage_ratio_basis_points` is the exact floor of
  `10000 * reserves / liabilities`, or `null` (it was a float-derived integer and the sentinel
  9999900).
- The legacy `ProofOfReserves` no longer nets negative customer balances: an overdrawn customer
  has no legacy leaf and is left out of the liabilities total; the count and total magnitude of
  such balances are in two new fields and summary keys, `user_receivables_count` and
  `user_receivables_total` (present in every summary, 0 when there is none).
- `MerkleProof.verify()` takes an optional `expected_root` and returns `False` for malformed
  input. Without the argument it only shows that a proof is consistent with the root it carries.
- `Money` refuses `bool` amounts.
- The legacy Merkle tree's bytes are unchanged, so roots of the intent and execution artifacts
  and of legacy proofs without negative balances stay the same.
- The source distribution also contains `tests/vectors/` and `conftest.py` (new
  `MANIFEST.in`): the tests it ships read the vectors, and they run from the unpacked archive
  as they do in the repository. The wheel still contains no tests.

### Fixed
- `MerkleProof.verify()` rejected every genuine proof with at least one step.
- The SQLite store raised `OverflowError` above 2**63 - 1 minor units (10 units of an
  18-decimal currency).
- As-of balances compared timestamp text in SQL, so a cut-off or a stored settlement in another
  UTC offset gave a wrong balance in either direction.
- A negative customer balance lowered the published liabilities and could turn the coverage
  verdict positive (+100 and -60 with reserves 50 were published as liabilities 40, covered).
- `coverage_ratio_basis_points` was off by one for some values because it was derived from a
  float.
- `TransactionBuilder.fx_convert` built transactions that could never be posted.
- For reserve artifacts (the new tree): the total and the leaf count are bound to the root, and
  without its salt a leaf hash can no longer be tested against a guessed identifier and
  balance. A proof still shows the exact sums of its sibling subtrees, usually one other
  account's amount (documented in the design, section 6.7). The legacy tree keeps its known
  limit (two leaf lists with different totals can share a root); a strict expected-failure test
  keeps it visible.

### Deprecated
- `ProofOfReservesGenerator.generate`, `ProofOfSolvencyGenerator.generate` and
  `verify_proof_with_node_prefix` emit a `PendingDeprecationWarning`. Replacements:
  `ReserveCoverageGenerator`, `DeclaredReserveCoverageGenerator` and
  `MerkleProof.verify(expected_root=...)`. The warning becomes a `DeprecationWarning` in the
  release that moves the README and the guides to the new names; the old names are removed no
  earlier than two minor releases after that.

### Notes
- No new runtime dependencies (standard library only). The new modules keep the project's
  determinism discipline (explicit time only; never `datetime.now()` on a proof/settlement
  path), verified by static (AST) and runtime guards.
- A successful verification establishes only the integrity and arithmetic checks documented
  for that artifact. It does not establish that every liability was recorded, that reserve
  assets exist or are available, or that an institution is solvent, and nothing here claims to
  satisfy any regulation.

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
