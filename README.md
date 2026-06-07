# openreserve

[![CI](https://github.com/Hiroshi-Ichiyanagi/openreserve/actions/workflows/ci.yml/badge.svg)](https://github.com/Hiroshi-Ichiyanagi/openreserve/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

A small, self-contained **verification core** for deterministic, offline-verifiable
proof-of-reserves and a tamper-evident audit chain. It is early and has **no production
adoption** yet — see [Status](#status).

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
"chain" here is a local hash-linked audit log, not a distributed ledger.

## What it does NOT do

- No HTTP server / public endpoint (artifacts are data; serving is the integrator's job).
- No payment, payout, FX-conversion, KYC, or business-application orchestration logic.
- No external network or cloud dependency at runtime (SQLite + standard library only).
- No on-chain anchoring or third-party attestation (could be layered on top).

## Status

- **Early / unproven.** No production deployments. APIs may change.
- Identifiers (`transaction_id`, `account_id`) are currently UUID-based, so they vary
  across independent runs; determinism guarantees here cover the **time axis** (fixed
  ledger state + fixed time → identical artifacts), not identifier stability.

## Install / requirements

- Python 3.11+
- Runtime dependencies: **none** (standard library only — `sqlite3`, `hashlib`, …).
- Test dependency: `pytest` (via the `test` extra).

```bash
pip install -e .            # editable install
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
