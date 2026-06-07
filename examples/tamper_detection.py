"""Show that the hash-linked audit chain detects tampering.

We build an audit log, confirm it verifies, then simulate an attacker editing a stored
event directly in the SQLite file (bypassing the API) and confirm verify_chain() catches
it.

Run: python examples/tamper_detection.py
"""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openreserve import AuditLog

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "audit.db")

        # Build a small chain with explicit timestamps.
        log = AuditLog(db)
        log.append("account_opened", {"account_id": "a1"}, timestamp=T)
        log.append("transaction_posted", {"tx": "t1", "amount": 300_000}, timestamp=T + timedelta(hours=1))
        log.append("transaction_settled", {"tx": "t1"}, timestamp=T + timedelta(hours=2))
        ok, err = log.verify_chain()
        print("before tampering: verify_chain ->", ok, err)
        log.close()

        # Attacker edits a stored event directly in the database (not via the API).
        con = sqlite3.connect(db)
        con.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"tx": "t1", "amount": 999999999}', 1),
        )
        con.commit()
        con.close()
        print("attacker rewrote event #1 payload (300000 -> 999999999)")

        # Re-open and re-verify: the chain no longer recomputes to the stored hashes.
        log2 = AuditLog(db)
        ok2, err2 = log2.verify_chain()
        print("after tampering:  verify_chain ->", ok2)
        print("  reason:", err2)
        log2.close()

        assert ok is True and ok2 is False, "expected detection of the tamper"
        print("\ntamper detected as expected.")


if __name__ == "__main__":
    main()
