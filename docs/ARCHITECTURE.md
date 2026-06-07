# Architecture

openreserve is a small verification core. This document describes how the pieces fit
together, the trust model, and where to extend it.

## Modules

```
openreserve/
  core/           append-only double-entry ledger + value types + SQLite storage
  transparency/   Merkle tree, hash-linked audit log, proof generators
  regulatory/     point-in-time reserve / deposit calculation
  providers/      abstract provider interface (ProviderCategory only; no implementations)
```

- **core** — `Money`, `Currency`, `Account`, `Transaction`, `TransactionBuilder`, the
  `Ledger`, and a `LedgerStorage` protocol with a `SQLiteLedgerStorage` implementation.
  Balances are derived from settled entries as of a given time.
- **transparency** — `MerkleTree` / `MerkleProof`, the `AuditLog` (hash chain), and the
  proof generators: `ProofOfReservesGenerator`, `ProofOfSolvencyGenerator`,
  `ProofOfComplianceGenerator`.
- **regulatory** — `PaymentServicesActCompliance`, which computes a point-in-time
  required reserve (settled user balances plus in-flight obligations, negative balances
  excluded) and returns structured results.
- **providers** — `ProviderCategory` and the abstract `LicensedProvider` interface. No
  concrete providers ship here; integrators supply their own.

## Dependency direction

The dependency graph is a one-way DAG; nothing in the core depends on an application or
business layer:

```
proofs ─┐
        ├─> core.ledger ─> core.storage ─> core.types
PSA ────┘
merkle      (standalone, standard library only)
audit_log   (standalone, standard library only)
```

`transparency.merkle` and `transparency.audit_log` have no internal dependencies.
`transparency.proofs` and `regulatory.payment_services_act` read from a `Ledger`. The
`Ledger` reads from a `LedgerStorage`. There are no cycles, and there is no dependency
on any serving layer or external service.

## Trust model

Inputs: a **ledger state** (via `Ledger` / `LedgerStorage`) and an **explicit point in
time** (`snapshot_at` / event timestamps).

Outputs: artifacts that are plain data. A `ProofOfReserves` exposes
`to_public_summary()` returning a JSON-serializable dict; a user inclusion proof is a
`MerkleProof`; the audit chain commitment is `AuditLog.latest_hash()`.

Because artifacts are data, a proof can be published (for example at a static URL) and a
third party can re-verify it offline using this library — no access to the operator's
running system is required. See [VERIFYING.md](VERIFYING.md).

The audit log is tamper-evident: each event hashes the previous event's hash, so editing
a past event invalidates every subsequent hash. `AuditLog.verify_chain()` detects this.

## Determinism

Proof and audit generation take the as-of / event time as an explicit, required input
and never read the wall clock (`datetime.now()`). Given the same ledger state and the
same time, the Merkle root and audit hashes are identical.

The guarantee is on the **time axis**. Account and transaction identifiers are
UUID-based, so two independently rebuilt ledgers do not share identifiers or hashes;
identifier stability across rebuilds is not claimed (see [ROADMAP.md](../ROADMAP.md)).
`tests/test_determinism_guard.py` exercises this contract, including a check that
generation never calls the wall clock.

## Extension points

- **Providers** — implement `LicensedProvider` (or supply your own object exposing the
  needed surface) to integrate a payout/transfer backend. The core only references
  `ProviderCategory`; concrete behavior is the integrator's seam.
- **Storage** — implement the `LedgerStorage` protocol to back the ledger with something
  other than the bundled SQLite store.
- **Reserve models** — `regulatory` is where additional point-in-time reserve / deposit
  rules can be added alongside the existing calculation.
- **Serving** — there is intentionally no HTTP layer. A reference serving layer that
  publishes proofs over HTTP could be built on top without changing the core.

## What is intentionally out of scope

No HTTP server, no payment/payout/FX/KYC orchestration, no external network or cloud
dependency at runtime, and no on-chain anchoring. openreserve is the verification core;
these belong in layers built around it.
