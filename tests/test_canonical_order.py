"""Artifacts do not depend on the order or the time in which accounts were opened (design
sections 6.2 and 8.3, the part of D22 that stage A covers).

Leaves of the Merkle-sum tree are ordered by their salted leaf hash, never by creation time
or storage order, and conversion transaction ids are derived from the declaration and the
source account. So the same balances give the same snapshot, the same coverage summary and
the same conversion evidence, however the accounts were opened. Stored account and
transaction ids are unchanged by this; content-addressed export ids are a later stage.
"""

from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openreserve.conversion import close_conversion, load_declaration, post_conversion_batch
from openreserve.conversion_evidence import build_conversion_evidence
from openreserve.core.exact import instant_from_text
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Account, Currency, Money, OwnerType
from openreserve.transparency.coverage import ReserveCoverageGenerator
from openreserve.transparency.snapshot import LiabilitiesSnapshotGenerator

VECTORS = Path(__file__).parent / "vectors"
EXPECTED = json.loads((VECTORS / "snapshot_expected_v1.json").read_text(encoding="utf-8"))
FIXTURE = json.loads((VECTORS / "conversion_ledger_fixture_v1.json").read_text(encoding="utf-8"))
KEY = bytes(range(32))
INSTITUTION = "example-institution"
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
ABC = {"alice": 300_000, "bob": 120_000, "carol": 55_000}


def _ledger():
    storage = SQLiteLedgerStorage(":memory:")
    return storage, Ledger(storage)


def _open(storage, account_id, owner_type, currency, created_at):
    storage.save_account(
        Account(account_id=account_id, owner_type=owner_type, currency=currency, label=account_id, created_at=created_at)
    )


def _move(ledger, src, dst, amount, currency, at):
    b = TransactionBuilder("MOVE", "test", initiated_at=at)
    b.transfer(src, dst, Money(cents=amount, currency=currency))
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=at)


def abc_ledger(open_order, created_at_step):
    """The abc fixture with a reserve, the accounts opened in ``open_order``, each one
    ``created_at_step`` later than the one before, and funded in the reverse order."""
    storage, ledger = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM, Currency.JPY, T - timedelta(days=10))
    for i, account_id in enumerate(open_order):
        account_type = OwnerType.RESERVE if account_id == "reserve" else OwnerType.USER
        _open(storage, account_id, account_type, Currency.JPY, T - timedelta(days=5) + i * created_at_step)
    for account_id in reversed(open_order):
        _move(ledger, "platform", account_id, 1_000_000 if account_id == "reserve" else ABC[account_id], Currency.JPY, T)
    return ledger


def test_snapshot_and_coverage_do_not_depend_on_when_or_in_which_order_accounts_were_opened():
    snapshot_hashes, coverage_hashes, storage_orders = set(), set(), set()
    for open_order in itertools.permutations(("alice", "bob", "carol", "reserve")):
        for step in (timedelta(0), timedelta(hours=1), -timedelta(minutes=7)):
            ledger = abc_ledger(open_order, step)
            storage_orders.add(tuple(a.account_id for a in ledger.list_accounts(owner_type=OwnerType.USER)))
            snapshot = LiabilitiesSnapshotGenerator(ledger, institution_id=INSTITUTION, salt_key=KEY).generate(Currency.JPY, T)
            coverage = ReserveCoverageGenerator(ledger, institution_id=INSTITUTION, salt_key=KEY).generate(Currency.JPY, T)
            snapshot_hashes.add(snapshot.summary_hash())
            coverage_hashes.add(coverage.summary_hash())
    assert len(storage_orders) == 6  # the accounts really came back in every order
    assert snapshot_hashes == {EXPECTED["abc_snapshot_summary"]["summary_hash"]}
    assert coverage_hashes == {EXPECTED["abc_reserve_coverage_summary"]["summary_hash"]}


def conversion_ledger(order, created_at_step):
    """The ledger fixture of the conversion tests, with its accounts opened in ``order``."""
    storage, ledger = _ledger()
    currencies = {"JPY": Currency.JPY, "USD": Currency.USD}
    funded = instant_from_text(FIXTURE["funding_instant"])
    accounts = {a["account_id"]: a for a in FIXTURE["accounts"]}
    for i, account_id in enumerate(order):
        a = accounts[account_id]
        _open(storage, account_id, OwnerType(a["account_type"]), currencies[a["currency"]], funded - timedelta(days=1) + i * created_at_step)
    for src, dst, amount in reversed(FIXTURE["funding_transfers_jpy"]):
        _move(ledger, src, dst, int(amount), Currency.JPY, funded)
    return ledger


def test_conversion_ids_and_evidence_do_not_depend_on_the_order_in_which_accounts_were_opened():
    declaration = load_declaration(FIXTURE["declaration"])
    effective = instant_from_text(FIXTURE["effective_at"])
    ids = [a["account_id"] for a in FIXTURE["accounts"]]
    accounts = dict(clearing_source_account_id="acct-clearing-jpy", clearing_target_account_id="acct-clearing-usd")
    manifest_hashes = set()
    for order, step in ((ids, timedelta(0)), (ids[::-1], timedelta(seconds=1)), (ids[1::2] + ids[0::2], -timedelta(hours=3))):
        ledger = conversion_ledger(order, step)
        result = post_conversion_batch(
            ledger, declaration, pairs=[tuple(p) for p in reversed(FIXTURE["conversion_pairs"])],
            effective_at=effective, initiator_id="ops", **accounts,
        )
        assert result.zero_balance == ("acct-erin",)  # nothing to post for a zero balance
        close_conversion(ledger, declaration, residue_account_id="acct-residue-usd", effective_at=effective,
                         initiator_id="ops", **accounts)
        for source, transaction_id in FIXTURE["expected_conversion_transaction_ids"].items():
            if source in result.zero_balance:
                continue
            assert ledger.get_transaction(transaction_id).entries[0].account_id == source
        evidence = build_conversion_evidence(
            ledger, declaration, institution_id=FIXTURE["institution_id"], salt_key=bytes.fromhex(FIXTURE["salt_key_hex"]),
            residue_account_id="acct-residue-usd", effective_at=effective, **accounts,
        )
        manifest_hashes.add(evidence.manifest_hash)
    assert manifest_hashes == {FIXTURE["expected_manifest_hash"]}
