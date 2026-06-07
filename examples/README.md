# Examples

Runnable scripts using only the public `openreserve` API. Each is self-contained and
uses in-memory or temporary storage, so they leave nothing behind.

```bash
python examples/proof_of_reserves.py        # generate a proof; a user verifies inclusion
python examples/publish_and_verify_offline.py  # publish JSON, then verify it offline
python examples/tamper_detection.py         # the audit chain detects a tampered event
python examples/reproducibility.py          # same inputs + same time -> same hash
```

| Script | Shows |
| ------ | ----- |
| `proof_of_reserves.py` | Build a ledger, generate a proof-of-reserves, print the public summary (no individual balances), and have one user verify their own inclusion via a Merkle proof. |
| `publish_and_verify_offline.py` | Serialize a proof and a user's Merkle proof to JSON, then re-verify solvency and inclusion in a step that reads only the JSON. |
| `tamper_detection.py` | Build a hash-linked audit log, edit a stored event directly in the database, and show `verify_chain()` catches it. |
| `reproducibility.py` | The same ledger state and the same `snapshot_at` produce identical hashes; the timestamp is required (no wall-clock fallback). |

See [../docs/VERIFYING.md](../docs/VERIFYING.md) for the verification model these
examples demonstrate.
