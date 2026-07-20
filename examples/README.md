# Examples

Runnable scripts using only the public `openreserve` API. Each is self-contained and
uses in-memory or temporary storage, so they leave nothing behind.

```bash
python examples/proof_of_reserves.py        # generate a proof; a user verifies inclusion
python examples/publish_and_verify_offline.py  # publish JSON, then verify it offline
python examples/tamper_detection.py         # the audit chain detects a tampered event
python examples/reproducibility.py          # same inputs + same time -> same hash
python examples/intent_mandate_settlement.py   # autonomous budget-bounded settlement + proof
python examples/best_execution_recapture.py     # solver auction + surplus recapture (3<->4 bridge)
python examples/session_key_submandate.py       # session-key sub-mandates with provable non-leakage
```

| Script | Shows |
| ------ | ----- |
| `proof_of_reserves.py` | Build a ledger, generate a proof-of-reserves, print the public summary (no individual balances), and have one user verify their own inclusion via a Merkle proof. |
| `publish_and_verify_offline.py` | Serialize a proof and a user's Merkle proof to JSON, then re-verify solvency and inclusion in a step that reads only the JSON. |
| `tamper_detection.py` | Build a hash-linked audit log, edit a stored event directly in the database, and show `verify_chain()` catches it. |
| `reproducibility.py` | The same ledger state and the same `snapshot_at` produce identical hashes; the timestamp is required (no wall-clock fallback). |
| `intent_mandate_settlement.py` | Grant a pre-funded mandate (budget, allowed purposes, window, per-tx cap), settle M2M intents autonomously with every accept/deny on a tamper-evident audit chain, then produce a `ProofOfMandate` proving the agent never exceeded budget — and let a payee verify its own receipts' inclusion. |
| `best_execution_recapture.py` | Fill an intent through a deterministic solver auction, split the price-improvement surplus (user rebate / operator margin / gas) in one balanced transaction, and emit a best-execution proof + a surplus-recapture proof. Shows the two value planes: the mandate's budget only ever sees the user's limit (INV-9), while the payee receives the better executed cost. |
| `session_key_submandate.py` | Delegate bounded sub-budgets to two session keys drawing on one root mandate; every ancestor's budget is re-derived from the ledger on each settlement, so combined descendant spend can never exceed the parent (`PARENT_BUDGET_EXCEEDED`). |

See [../docs/VERIFYING.md](../docs/VERIFYING.md) for the verification model these
examples demonstrate.
