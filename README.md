# openreserve

[![CI](https://github.com/Hiroshi-Ichiyanagi/openreserve/actions/workflows/ci.yml/badge.svg)](https://github.com/Hiroshi-Ichiyanagi/openreserve/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openreserve)](https://pypi.org/project/openreserve/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

A small, self-contained **verification core** for deterministic, offline-verifiable
proof-of-reserves and a tamper-evident audit chain. It is early and has **no production
adoption** yet — see [Limitations](#limitations).

## What it does

Given a **ledger state** (an append-only double-entry ledger) and an **explicit
point in time**, it produces and verifies:

- **Proof of Reserves** — a Merkle tree over user-account balances plus reserve-account
  totals, with a solvency check (`reserves >= liabilities`). Each user can verify their
  own balance is included via a Merkle proof, without the operator revealing other users'
  balances.
- **Proof of Solvency** — per-currency proof-of-reserves aggregated with the audit-chain
  commitment hash.
- **Audit chain** — a hash-linked event log (`prev_hash` → `event_hash`) with
  `verify_chain()` for end-to-end integrity / tamper detection.
- **Reserve / deposit calculation** — point-in-time required-reserve computation
  (settled user balances + in-flight obligations, negative balances excluded).

All artifacts are plain data (`to_public_summary()` returns JSON-serializable dicts).
A produced proof can be published at a static URL and **independently re-verified
offline** by a third party using this library — e.g. fetch the JSON with `curl` and
re-run the Merkle / chain verification locally. **This core does not ship an HTTP
serving layer**; exposing an endpoint is left to the integrator.

## Use cases (who it's for)

It targets operators whose ledger is off-chain and whose auditors, regulators, or
counterparties are off-chain too — i.e. where an on-chain oracle is the wrong shape.

- **E-money / prepaid-balance issuers** — show that stored balances are backed.
- **Payment processors (PSPs)** — prove safeguarded customer funds against liabilities.
- **Custodians** — including a crypto exchange's off-chain customer ledger (the CEX
  proof-of-reserves case), on the bookkeeping side rather than the on-chain side.
- **Stablecoin issuers** — attestation of the off-chain reserve side (not the on-chain token).
- **Loyalty-point / closed-loop wallet operators** — prove outstanding point/credit
  balances are covered.

## Proof of Reserves vs Proof of Solvency

- **Proof of Reserves** — evidence that the reserve assets exist (and, here, a
  Merkle-committed view of the liabilities they back).
- **Proof of Solvency** — evidence that `assets >= liabilities`, i.e. that the reserves
  actually cover what is owed.

openreserve provides both: a Merkle-committed reserve proof plus a per-currency solvency
check. The solvency result is only as complete as the set of liabilities you include in
the ledger — see [Limitations](#limitations).

## Determinism is the core property

Proof generation takes the as-of / event time as an **explicit input** and never reads
the wall clock (`datetime.now()`). Identical inputs + identical explicit time produce a
byte-identical artifact hash. This is what makes a published proof reproducible by anyone.

## "Verify me"

Don't take the above on trust — the bundled guard checks it:

```bash
python -m pytest tests/test_determinism_guard.py -v
```

It asserts (a) the same ledger state + same `snapshot_at` reproduces the same Merkle
root and audit hash, and (b) with `datetime.now` patched to raise, generation with an
explicit time still succeeds (the wall clock is never touched).

### Verification flow

```
OPERATOR SIDE
  ledger state + explicit as-of time
        |
        v
  generate proof            (deterministic: same state + time -> same hashes)
        |
        v
  publish proof as JSON     (static URL or file)
        |
========|=====================================================
        v
VERIFIER SIDE  (auditor / user / regulator -- offline, no trust in operator)
  fetch proof JSON
        |
        +--> recompute Merkle root
        +--> verify audit chain   (prev_hash -> event_hash)
        +--> check solvency       (reserves >= liabilities)
        +--> (a user) verify own balance is included via Merkle proof
```

See [docs/VERIFYING.md](docs/VERIFYING.md) for the step-by-step verifier guide.

Run the full suite:

```bash
python -m pytest -q
```

## Usage

```python
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

# A fixed, explicit point in time — proofs never read the wall clock.
T = datetime(2026, 1, 1, tzinfo=timezone.utc)

ledger = Ledger(SQLiteLedgerStorage(":memory:"))
platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "platform")
reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "reserve")
alice = ledger.open_account(OwnerType.USER, Currency.JPY, "alice")


def fund(src, dst, units):
    b = TransactionBuilder("FUND", "ops", initiated_at=T)
    b.transfer(src.account_id, dst.account_id, Money.from_units(units, Currency.JPY))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=T)


fund(platform, reserve, 1_000_000)  # reserve assets
fund(platform, alice, 300_000)      # a user balance (liability)

gen = ProofOfReservesGenerator(ledger)
proof, _tree = gen.generate(currency=Currency.JPY, snapshot_at=T)

print("solvent:     ", proof.is_solvent)                    # True
print("liabilities: ", proof.user_liabilities_total_cents)  # 300000
print("reserves:    ", proof.reserve_assets_total_cents)    # 1000000
print("merkle root: ", proof.user_liabilities_root[:16] + "...")

# Determinism: same ledger state + same snapshot_at -> identical Merkle root.
proof2, _ = gen.generate(currency=Currency.JPY, snapshot_at=T)
print("reproducible:", proof.user_liabilities_root == proof2.user_liabilities_root)  # True
```

## Positioning

This targets **non-crypto / TradFi** use: a payment, wallet, or custody operator proving
that safeguarded customer funds are backed by reserves, as a point-in-time, auditable,
offline-verifiable artifact — **without requiring an on-chain oracle**. Reserve proof is
the primary use case.

It is **not** an on-chain proof-of-reserves system and is **not** positioned to compete
with on-chain PoR oracles (e.g. Chainlink). There is no blockchain dependency; the
"chain" here is a local hash-linked audit log, not a distributed ledger. It is also
**not a compliance product** (no claim to satisfy any regulation; nothing here is legal
or financial advice) and **not novel cryptography** — it is a clean assembly of standard
primitives (Merkle trees, hash chains) under a determinism discipline.

## Limitations

- **Determinism is on the time axis.** Identifiers (`transaction_id`, `account_id`) are
  UUID-based, so they vary across independent runs; the guarantee is that a fixed ledger
  state at a fixed time produces identical artifacts, not identifier stability across
  rebuilds. Content-addressed identifiers are a [roadmap](ROADMAP.md) item.
- **Liabilities completeness.** A solvency check only covers the liabilities present in
  the ledger. Per-user Merkle inclusion raises the cost of omitting a liability (an
  omitted user cannot find their balance), but it is not an attestation that the ledger
  contains *every* liability.
- **No HTTP / serving layer.** Artifacts are data; publishing an endpoint is the
  integrator's job.
- **No on-chain anchoring.** Nothing is committed to a blockchain; this can be layered on
  top if an external anchor is wanted.
- **No payment/payout/FX/KYC/business logic** and no external network or cloud dependency
  at runtime (SQLite + standard library only).
- **Early and unproven.** v0.1.1, no production adoption, APIs may change.

## Install / requirements

- Python 3.11+
- Runtime dependencies: **none** (standard library only — `sqlite3`, `hashlib`, …).
- Test dependency: `pytest` (via the `test` extra).

```bash
pip install openreserve
```

From source (for development):

```bash
pip install -e ".[test]"    # editable install + pytest
python -m pytest -q         # run the suite
```

## Layout

```
openreserve/
  __init__.py     public API re-exports (+ __version__)
  core/           append-only double-entry ledger + types + SQLite storage
  transparency/   merkle tree, hash-chain audit log, proof generators
  regulatory/     point-in-time reserve / deposit calculation
  providers/      abstract provider interface (ProviderCategory only; no implementations)
tests/            unit tests + determinism guard
```

## Project

- [Architecture](docs/ARCHITECTURE.md) — modules, dependency graph, trust model, extension points
- [Verifying a proof](docs/VERIFYING.md) — re-verify a published proof offline ("verify me")
- [Examples](examples/) — runnable scripts (proof-of-reserves, offline verify, tamper detection, reproducibility)
- [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md)
- [Roadmap](ROADMAP.md) — directions (exploratory, not promises) · [Changelog](CHANGELOG.md)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
